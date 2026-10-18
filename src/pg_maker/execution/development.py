"""Durable diagnostic commands in a private copy of a completed build workspace."""

import os
import shutil
from pathlib import Path

from pg_maker.errors import CancelledError, MakerError, PreconditionError
from pg_maker.execution.build import BuildBranch
from pg_maker.execution.cancellation import process_identity
from pg_maker.execution.preparation import PreparationContext
from pg_maker.execution.resources import check_resources
from pg_maker.execution.state import OperationStore
from pg_maker.models import TERMINAL_STATUSES, parse_request
from pg_maker.progress import ProgressReporter
from pg_maker.runners.docker import DockerRunner
from pg_maker.storage import content_hash, exclusive_lock, read_json, write_json


def execute_command(directory, token):
    branch, error, result = None, None, None
    attempt = OperationStore(directory).load()["attempt"]
    with ProgressReporter(
        directory / "attempts" / str(attempt) / "progress.log",
        operation_id=directory.name,
        attempt=attempt,
    ) as progress:
        context = PreparationContext(directory, token, progress)
        try:
            document = read_json(directory / "input.json")
            if content_hash(document) != context.state["input_hash"]:
                raise PreconditionError("development command input changed")
            request = parse_request(document)
            context.event(
                "operation_started", status="running", owner=process_identity(os.getpid())
            )
            source = Path(request.source_run)
            from pg_maker.api import MakerService

            MakerService._reference(source)
            runner = DockerRunner(
                directory,
                operation_id=directory.name,
                owner_token=context.state["owner_token"],
                cancelled=token.requested,
                on_process=context.on_process,
                attempt=attempt,
            )
            with exclusive_lock(source / "owner.lock", blocking=False):
                state, previous = read_json(source / "state.json"), read_json(source / "input.json")
                if state["status"] not in TERMINAL_STATUSES or state["resources"]:
                    raise PreconditionError("debug source run is active or still owns resources")
                if (
                    content_hash(previous) != request.source_input_hash
                    or state["input_hash"] != request.source_input_hash
                ):
                    raise PreconditionError("debug source input changed")
                prior = parse_request(previous)
                if request.step not in prior.plan["spec"]["steps"]:
                    raise PreconditionError("debug environment is not part of the source plan")
                branch = BuildBranch(context, runner, prior, request.step)
                old_root = source / "workspace/build" / branch.id
                if not old_root.is_dir() or old_root.is_symlink():
                    raise PreconditionError("debug source scratch is missing")
                size = sum(
                    p.stat().st_size
                    for p in old_root.rglob("*")
                    if p.is_file() and not p.is_symlink()
                )
                check_resources(
                    directory, branch.config["defaults"]["execution"]["limits"], extra_bytes=size
                )
                shutil.copytree(old_root, branch.root, symlinks=True)
            branch.begin()
            branch.prepare()
            cwd = branch.container_root + "/" + request.cwd
            output = branch.command(list(request.argv), cwd=cwd, timeout=request.timeout_seconds)
            result = {
                "stdout_log": output.stdout_log,
                "stderr_log": output.stderr_log,
                "returncode": output.returncode,
                "qualification": "unverified",
                "source_run": request.source_run,
            }
            write_json(branch.output / "debug.json", result)
        except MakerError as exc:
            error = exc
        except Exception as exc:
            error = MakerError(f"{type(exc).__name__}: {exc}")
        finally:
            if branch:
                try:
                    branch.release()
                except MakerError as exc:
                    error = error or exc
        status = (
            "succeeded"
            if error is None
            else "cancelled"
            if isinstance(error, CancelledError)
            else "failed"
        )
        context.event(
            "operation_finished",
            status=status,
            result=result,
            error=error.as_dict() if error else None,
            owner=None,
        )
        return 0 if error is None else error.exit_code
