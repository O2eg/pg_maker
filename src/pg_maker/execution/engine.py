"""Operation dispatch and sequential container-command engine."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from pg_maker.errors import CancelledError, CommandError, MakerError, PreconditionError
from pg_maker.execution.cancellation import CancellationToken, process_identity
from pg_maker.execution.state import OperationStore
from pg_maker.models import CleanupPolicy, OperationRequest, OutputSpec
from pg_maker.progress import ProgressReporter
from pg_maker.runners.docker import DockerRunner
from pg_maker.storage import content_hash, exclusive_lock, read_json


def execute_operation(directory: Path, token: CancellationToken) -> int:
    import os

    kind = read_json(directory / "input.json").get("operation_kind")
    if kind == "build":
        from pg_maker.execution.build import execute_build

        return execute_build(directory, token)
    if kind == "development_command":
        from pg_maker.execution.development import execute_command

        return execute_command(directory, token)
    if kind != "container_commands":
        from pg_maker.execution.preparation import execute_preparation

        return execute_preparation(directory, token)

    store = OperationStore(directory)
    with exclusive_lock(directory / "control.lock"):
        state = store.load(repair=True)

    def event(event_type: str, **changes: Any) -> dict[str, Any]:
        nonlocal state
        with exclusive_lock(directory / "control.lock"):
            state = store.record(event_type, **changes)
            return state

    def on_process(identity: dict[str, Any]) -> None:
        resources = [item for item in state["resources"] if item["kind"] != "process"]
        event("process_started", resources=[*resources, {"kind": "process", "identity": identity}])

    runner: DockerRunner | None = None
    handle = None
    results: list[dict[str, Any]] = []
    error: MakerError | None = None
    status, artifacts = "succeeded", []
    with ProgressReporter(
        directory / "attempts/1/progress.log", operation_id=state["operation_id"]
    ) as progress:
        try:
            payload = read_json(directory / "input.json")
            if content_hash(payload) != state["input_hash"]:
                raise PreconditionError("operation input hash changed")
            request = OperationRequest.from_document(payload)
            event("operation_started", status="running", owner=process_identity(os.getpid()))
            progress.configure(len(request.commands))
            progress.info("Operation started")
            if token.requested():
                raise CancelledError("operation cancelled before environment preparation")
            runner = DockerRunner(
                directory,
                operation_id=state["operation_id"],
                owner_token=state["owner_token"],
                cancelled=token.requested,
                on_process=on_process,
            )
            # Fail unavailable/unsupported environments before promising any Docker resource.
            runner.doctor()
            handle = runner.intended_handle(request.environment)
            # Record intent before Docker create so a killed worker leaves discoverable resources.
            event(
                "environment_preparing",
                resources=[
                    {
                        "kind": "docker",
                        "name": handle.name,
                        "labels": handle.labels,
                        "container_id": None,
                    }
                ],
            )
            handle = runner.prepare(request.environment)
            event(
                "environment_ready",
                resources=[
                    {
                        "kind": "docker",
                        "name": handle.name,
                        "labels": handle.labels,
                        "container_id": handle.container_id,
                    }
                ],
            )
            for command in request.commands:
                if token.requested():
                    raise CancelledError("operation cancelled")
                event(
                    "command_started", step_id=command.id, target_id=request.environment.target_id
                )
                progress.info("Command started", step_id=command.id)
                result = runner.execute(handle, command)
                document = asdict(result)
                for key in ("stdout_log", "stderr_log"):
                    document[key] = str(Path(document[key]).relative_to(directory))
                results.append({"id": command.id, **document})
                event(
                    "command_finished",
                    step_id=command.id,
                    result={"commands": results},
                    progress={"completed": len(results), "total": len(request.commands)},
                )
                if result.cancelled or token.requested():
                    raise CancelledError("operation cancelled")
                if not result.succeeded:
                    raise CommandError(
                        "command timed out" if result.timed_out else "command failed",
                        details={"command_id": command.id, "returncode": result.returncode},
                    )
                progress.advance("Command completed", step_id=command.id)
            # Stop any remaining container writers before verifying immutable output bytes.
            event("outputs_verifying")
            runner.cancel(handle)
            artifacts = [
                asdict(item)
                for item in runner.collect(handle, OutputSpec(request.outputs)).artifacts
            ]
        except MakerError as exc:
            error = exc
            status = "cancelled" if isinstance(exc, CancelledError) else "failed"
        except Exception as exc:
            error, status = MakerError(f"{type(exc).__name__}: {exc}"), "failed"
        finally:
            if runner is not None and handle is not None:
                try:
                    event("cleanup_started")
                    runner.release(handle, CleanupPolicy.REMOVE)
                    event("cleanup_finished", resources=[])
                except Exception as exc:
                    # Preserve refs for reconciliation; cleanup failure cannot mean success.
                    error = MakerError(
                        "resource cleanup failed",
                        details={
                            "reason": str(exc),
                            "previous_error": error.as_dict() if error else None,
                        },
                    )
                    status = "failed"
            elif runner is not None:
                # Control subprocesses have been reaped; no container creation was attempted.
                event("preparation_stopped", resources=[])
            if token.requested() and status == "succeeded":
                error, status = CancelledError("operation cancelled"), "cancelled"
            event(
                "operation_finished",
                status=status,
                owner=None,
                result={"commands": results, "artifacts": artifacts},
                error=error.as_dict() if error else None,
            )
            if error:
                progress.error(str(error))
            else:
                progress.info("Operation completed")
    return error.exit_code if error else 0
