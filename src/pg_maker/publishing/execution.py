"""Detached publication worker with preflight, immutable plans and remote reconciliation."""

import os
import time
from pathlib import Path

from pg_maker.documents import validate_document
from pg_maker.errors import CancelledError, MakerError, PreconditionError
from pg_maker.execution.cancellation import process_identity
from pg_maker.execution.preparation import PreparationContext
from pg_maker.execution.state import OperationStore
from pg_maker.models import TERMINAL_STATUSES, parse_request
from pg_maker.progress import ProgressReporter
from pg_maker.publishing.artifacts import plan, source_state
from pg_maker.publishing.receivers import ADAPTERS
from pg_maker.publishing.transport import pause
from pg_maker.storage import content_hash, file_hash, read_json, write_json


def publish(request, context):
    from pg_maker.api import MakerService

    source = Path(request.source)
    deadline = time.monotonic() + request.config["source_timeout_seconds"]
    while True:
        context.check()
        state = source_state(source)
        if state["input_hash"] != request.source_input_hash:
            raise PreconditionError("publication source operation changed")
        if not (source / "export-manifest.json").exists():
            state = MakerService(source.parent).status(source)
        if state["status"] in TERMINAL_STATUSES:
            break
        if not request.wait_for_source:
            raise PreconditionError("source build has not completed")
        if time.monotonic() >= deadline:
            raise PreconditionError("waiting for source build timed out")
        pause(0.2, context.check)
    document = plan(source, request.config)
    if request.source_plan_hash and request.source_plan_hash != document["plan_hash"]:
        raise PreconditionError("publication source changed after the request was created")
    output = context.directory / "workspace/output"
    output.mkdir(parents=True, exist_ok=True)
    planned = output / "publication-plan.json"
    if planned.exists() and read_json(planned) != document:
        raise PreconditionError("publication plan changed since the previous attempt")
    write_json(planned, document)
    context.event(
        "publication_planned", result={"source": request.source, "plan_hash": document["plan_hash"]}
    )
    receivers = {
        r["id"]: ADAPTERS[r["adapter"]](r, request.config, context.check)
        for r in request.config["repositories"]
    }
    # Check every receiver and conflict before the first write anywhere.
    for name, receiver in receivers.items():
        context.check()
        receiver.preflight(
            [d["package"] for d in document["destinations"] if d["receiver"] == name]
        )
    context.progress.configure(len(document["destinations"]))
    completed = []
    report = {
        "api_version": "pg_maker/publication-result/v1",
        "plan_hash": document["plan_hash"],
        "source": request.source,
        "status": "running",
        "artifacts": completed,
        "qualification": "unverified",
        "repository_installation_tests": "not-run",
    }
    report_path = output / "publication-result.json"
    try:
        for destination in document["destinations"]:
            context.check()
            package = destination["package"]
            name = destination["receiver"]
            receipt_path = output / "receipts" / name / (package["filename"] + ".json")
            context.event("publication_upload_started", step_id=name + "/" + package["filename"])
            result = receivers[name].publish(package)
            receipt = {"receiver": name, "package": package, **result}
            write_json(receipt_path, receipt)
            completed.append(receipt)
            write_json(report_path, report)
            context.advance("publication_asset_verified", step_id=name + "/" + package["filename"])
        context.check()
        report["status"] = "succeeded"
        report["qualification"] = "publication-verified"
    finally:
        if report["status"] != "succeeded":
            report["status"] = "incomplete"
        validate_document(report, "publication-result")
        write_json(report_path, report)
    return {
        "document": str(report_path),
        "sha256": file_hash(report_path),
        "plan_hash": document["plan_hash"],
        "source": request.source,
        "qualification": "publication-verified",
        "published": len(completed),
    }


def execute_publication(directory, token):
    state = OperationStore(directory).load()
    error = None
    status = "succeeded"
    result = None
    with ProgressReporter(
        directory / "attempts" / str(state["attempt"]) / "progress.log",
        operation_id=directory.name,
        attempt=state["attempt"],
    ) as progress:
        context = PreparationContext(directory, token, progress)
        try:
            payload = read_json(directory / "input.json")
            if content_hash(payload) != state["input_hash"]:
                raise PreconditionError("publication request changed")
            request = parse_request(payload)
            context.event(
                "operation_started",
                status="running",
                owner=process_identity(os.getpid()),
                result={"source": request.source},
            )
            result = publish(request, context)
        except MakerError as exc:
            error = exc
            status = "cancelled" if isinstance(exc, CancelledError) else "failed"
        except Exception as exc:
            # Network/server data and credential exceptions must never reach persisted logs.
            error = MakerError("publication failed", details={"exception_type": type(exc).__name__})
            status = "failed"
        if error:
            result = {"source": read_json(directory / "input.json")["source"]}
            report = directory / "workspace/output/publication-result.json"
            if report.is_file():
                result.update(document=str(report), sha256=file_hash(report))
            progress.error(str(error))
        context.event(
            "operation_finished",
            status=status,
            owner=None,
            resources=[],
            result=result,
            error=error.as_dict() if error else None,
        )
    return error.exit_code if error else 0
