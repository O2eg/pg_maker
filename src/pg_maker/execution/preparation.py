"""Durable worker adapter for environment preparation and input resolution."""

import os
import shutil
import uuid
from pathlib import Path

from pg_maker.errors import CancelledError, MakerError, PreconditionError
from pg_maker.execution.cancellation import process_identity
from pg_maker.execution.process import run_process
from pg_maker.execution.state import OperationStore
from pg_maker.models import parse_request
from pg_maker.progress import ProgressReporter
from pg_maker.storage import content_hash, exclusive_lock, file_hash, read_json, write_json


class PreparationContext:
    def __init__(self, directory, token, progress):
        self.directory, self.token, self.progress = directory, token, progress
        self.store = OperationStore(directory)
        self.state = self.store.load()
        self.attempt_directory = directory / "attempts" / str(self.state["attempt"])
        self.offline = False
        self.timeout = 1800

    def event(self, event_type, **changes):
        with exclusive_lock(self.directory / "control.lock"):
            self.state = self.store.record(event_type, **changes)
        return self.state

    def on_process(self, identity):
        resources = [r for r in self.state["resources"] if r["kind"] != "process"]
        self.event(
            "process_started", resources=[*resources, {"kind": "process", "identity": identity}]
        )

    def check(self):
        if self.token.requested():
            raise CancelledError("operation cancelled")

    def advance(self, name, **event):
        completed = self.state["progress"]["completed"] + 1
        total = max(completed, self.state["progress"]["total"])
        self.event(name, progress={"completed": completed, "total": total}, **event)
        self.progress.advance(name)

    def command(self, argv):
        self.check()
        result = run_process(
            argv,
            log_directory=self.attempt_directory / "logs",
            name="prepare-" + uuid.uuid4().hex,
            timeout_seconds=self.timeout,
            cancelled=self.token.requested,
            on_start=self.on_process,
        )
        self.check()
        if not result.succeeded:
            raise PreconditionError(
                "preparation command failed", details={"stderr": result.stderr_tail}
            )
        return result


def execute_preparation(directory, token):
    from pg_maker.planning.environments import prepare_environments
    from pg_maker.planning.resolve import resolve_inputs

    error, result, status = None, None, "succeeded"
    attempt = OperationStore(directory).load()["attempt"]
    with ProgressReporter(
        directory / "attempts" / str(attempt) / "progress.log",
        operation_id=directory.name,
        attempt=attempt,
    ) as progress:
        context = PreparationContext(directory, token, progress)
        try:
            payload = read_json(directory / "input.json")
            if content_hash(payload) != context.state["input_hash"]:
                raise PreconditionError("operation input hash changed")
            request = parse_request(payload)
            context.offline = request.offline
            limits = request.config["defaults"]["execution"]["limits"]
            context.timeout = limits["stage_timeout_seconds"]
            if shutil.disk_usage(directory).free < limits["min_free_disk_bytes"]:
                raise PreconditionError("insufficient free disk space for preparation")
            context.event(
                "operation_started", status="running", owner=process_identity(os.getpid())
            )
            progress.configure(1)
            progress.info("Preparation started")
            context.check()
            if request.operation_kind == "prepare_environments":
                document = prepare_environments(request.config, context)
            elif request.operation_kind == "runtime_image":
                from pg_maker.integrations.runtime import build_runtime

                document = build_runtime(request.plan, context)
            else:
                document = resolve_inputs(request, context)
            context.check()
            output = Path(request.output)
            write_json(output, document)
            result = {
                "document": str(output),
                "sha256": file_hash(output),
                "document_hash": document.get(
                    "document_hash", document.get("environment_hash", document.get("lock_hash"))
                ),
            }
        except MakerError as exc:
            error = exc
            status = "cancelled" if isinstance(exc, CancelledError) else "failed"
        except Exception as exc:
            error, status = MakerError(f"{type(exc).__name__}: {exc}"), "failed"
        # Processes have been joined by run_process; preserve Docker refs if cleanup failed.
        resources = [r for r in context.state["resources"] if r["kind"] != "process"]
        context.event(
            "operation_finished",
            status=status,
            owner=None,
            result=result,
            resources=resources,
            error=error.as_dict() if error else None,
        )
        if error:
            progress.error(str(error))
        else:
            progress.info("Preparation completed")
    return error.exit_code if error else 0
