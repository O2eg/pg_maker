"""Public CLI-independent service. All operation paths are explicit and absolute."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from pg_maker.errors import OwnershipError, PreconditionError, ValidationError
from pg_maker.execution.cancellation import process_identity, stop_process_group
from pg_maker.execution.events import read_events
from pg_maker.execution.state import OperationStore
from pg_maker.models import (
    TERMINAL_STATUSES,
    BuildRequest,
    CleanupPolicy,
    EnvironmentSpec,
    OperationRequest,
    PreparationRequest,
    RuntimeRequest,
    parse_request,
)
from pg_maker.runners.docker import DockerRunner
from pg_maker.storage import (
    content_hash,
    ensure_directory,
    exclusive_lock,
    private_open,
    read_json,
    utc_now,
    write_json,
)


class MakerService:
    def __init__(self, operations_directory: Path) -> None:
        if not operations_directory.is_absolute():
            raise ValidationError("operations_directory must be absolute")
        self.directory = operations_directory
        self._children: dict[str, subprocess.Popen] = {}

    @staticmethod
    def _reference(directory: Path) -> Path:
        if not directory.is_absolute() or not directory.is_dir():
            raise PreconditionError("operation reference must be an existing absolute directory")
        for path in (directory, *directory.parents):
            if path.is_symlink():
                raise OwnershipError("operation reference must not traverse symbolic links")
        if directory.stat().st_uid != os.getuid():
            raise OwnershipError("operation directory belongs to another uid")
        return directory

    def doctor(self) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="pg-maker-doctor-") as name:
            return DockerRunner(
                Path(name), operation_id="doctor", owner_token=uuid.uuid4().hex
            ).doctor()

    def dev_open(
        self,
        directory: Path,
        *,
        lock=None,
        from_run=None,
        feature=None,
        input_store=None,
        repository=None,
    ):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).open(
            lock=lock,
            from_run=from_run,
            feature=feature,
            input_store=input_store,
            repository=repository,
        )

    def dev_status(self, directory: Path):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).status()

    def dev_patch(self, directory: Path, action: str, **options):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).patch(action, **options)

    def dev_build(self, directory: Path, *, goal="build", components=None):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).build(goal=goal, components=components)

    def dev_explain(self, directory: Path, *, goal="build", components=None):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).explain(goal=goal, components=components)

    def dev_execute(self, directory: Path, argv, **options):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).execute(argv, **options)

    def dev_pin(self, directory: Path, iteration: str, *, pinned=True):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).pin(iteration, pinned=pinned)

    def dev_prune(self, directory: Path, *, keep_last=2, apply=None):
        from pg_maker.development.session import DevelopmentSession

        return DevelopmentSession(directory).prune(keep_last=keep_last, apply=apply)

    def start_operation(
        self,
        request: OperationRequest | PreparationRequest | BuildRequest | RuntimeRequest,
        *,
        operation_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        document = request.to_document()
        request = parse_request(document)
        if request_id is not None and (
            not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
        ):
            raise ValidationError("request_id must contain 1..256 characters")
        operation_id = operation_id or "op-" + uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", operation_id):
            raise ValidationError("invalid operation_id")
        input_hash = content_hash(document)
        ensure_directory(self.directory)
        with exclusive_lock(self.directory / ".start.lock"):
            index = (
                self.directory / ".requests" / (content_hash(request_id).split(":")[1] + ".json")
            )
            if request_id is not None and index.exists():
                previous = read_json(index)
                if previous["input_hash"] != input_hash:
                    raise PreconditionError("request_id was already used with different inputs")
                return self.status(self.directory / previous["operation_id"])
            directory = self.directory / operation_id
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError as exc:
                raise OwnershipError("operation_id already exists") from exc
            write_json(directory / "input.json", document)
            store = OperationStore(directory)
            created = utc_now()
            with exclusive_lock(directory / "owner.lock") as owner_fd:
                with exclusive_lock(directory / "control.lock"):
                    state = store.record(
                        "operation_created",
                        initial={
                            "api_version": "pg_maker/operation/v1",
                            "kind": "OperationState",
                            "operation_id": operation_id,
                            "operation_kind": request.operation_kind,
                            "input_hash": input_hash,
                            "request_id": request_id,
                            "status": "planned",
                            "owner": None,
                            "owner_token": uuid.uuid4().hex,
                            "attempt": 1,
                            "sequence": 0,
                            "created_at": created,
                            "updated_at": created,
                            "progress": {"completed": 0, "total": len(request.commands)},
                            "resources": [],
                            "result": None,
                            "error": None,
                        },
                    )
                    if request_id is not None:
                        write_json(index, {"operation_id": operation_id, "input_hash": input_hash})
                    try:
                        with os.fdopen(
                            private_open(directory / "worker.log", append=True), "wb"
                        ) as log:
                            child = subprocess.Popen(
                                [
                                    sys.executable,
                                    "-m",
                                    "pg_maker.execution.worker",
                                    "--directory",
                                    str(directory),
                                    "--owner-fd",
                                    str(owner_fd),
                                ],
                                stdin=subprocess.DEVNULL,
                                stdout=log,
                                stderr=log,
                                start_new_session=True,
                                pass_fds=(owner_fd,),
                            )
                        self._children[operation_id] = child
                        state = store.record("worker_started", owner=process_identity(child.pid))
                    except OSError as exc:
                        error = PreconditionError("cannot start operation worker")
                        store.record("worker_start_failed", status="failed", error=error.as_dict())
                        raise error from exc
            return state

    def status(self, directory: Path) -> dict[str, Any]:
        directory = self._reference(directory)
        child = self._children.get(directory.name)
        if child and child.poll() is not None:
            del self._children[directory.name]
        # A live lock is stronger than a PID check and closes the worker-start race.
        with ExitStack() as ownership:
            try:
                ownership.enter_context(exclusive_lock(directory / "owner.lock", blocking=False))
            except OwnershipError:
                with exclusive_lock(directory / "control.lock"):
                    return OperationStore(directory).load()
            with exclusive_lock(directory / "control.lock"):
                store = OperationStore(directory)
                state = store.load(repair=True)
                if state["status"] not in TERMINAL_STATUSES:
                    state = store.record(
                        "worker_lost",
                        status="interrupted",
                        owner=None,
                        error={
                            "code": "worker_lost",
                            "message": "operation worker is no longer running",
                            "details": {},
                        },
                    )
                return state

    def resume(
        self,
        directory: Path,
        *,
        plan_hash: str | None = None,
        input_hash: str | None = None,
    ) -> dict:
        from pg_maker.errors import UnsupportedError
        from pg_maker.planning.plan import build_plan

        directory = self._reference(directory)
        state = self.status(directory)
        if (directory / "scratch-cleaned.json").exists():
            raise PreconditionError(
                "operation scratch was cleaned; start a new operation from exported inputs"
            )
        if state["status"] not in {"failed", "cancelled", "interrupted", "partial"}:
            raise PreconditionError(
                "only failed, partial, cancelled or interrupted operations can resume"
            )
        document = read_json(directory / "input.json")
        if content_hash(document) != state["input_hash"]:
            raise PreconditionError("operation input hash changed")
        request = parse_request(document)
        if isinstance(request, BuildRequest):
            if plan_hash is None or plan_hash != request.plan["plan_hash"]:
                raise PreconditionError("resume requires the original --plan-hash")
            expected = build_plan(
                request.lock,
                goal=request.plan["spec"]["goal"],
                input_store=Path(request.input_store),
                run_db_tests=request.plan["spec"]["run_db_tests"],
                components=request.plan["spec"].get("components"),
            )
            if expected["plan_hash"] != plan_hash:
                raise PreconditionError("resume plan differs from the current required graph")
        elif isinstance(request, RuntimeRequest):
            from pg_maker.integrations.runtime import verify_runtime_plan

            verify_runtime_plan(request.plan, plan_hash)
        elif isinstance(request, PreparationRequest):
            if input_hash is None or input_hash != state["input_hash"]:
                raise PreconditionError("preparation resume requires the original --input-hash")
        else:
            raise UnsupportedError("arbitrary container command operations cannot resume")
        # Reconcile only the exact recorded resources, before granting a new attempt ownership.
        self.cancel(directory)
        with exclusive_lock(directory / "owner.lock", blocking=False) as owner_fd:
            with exclusive_lock(directory / "control.lock"):
                store = OperationStore(directory)
                state = store.load(repair=True)
                if (
                    state["status"] not in {"failed", "cancelled", "interrupted", "partial"}
                    or state["resources"]
                ):
                    raise OwnershipError("operation changed or still owns live resources")
                attempt = state["attempt"] + 1
                previous = directory / "attempts" / str(state["attempt"])
                write_json(previous / "state.json", state)
                if (directory / "cancel.json").exists():
                    os.replace(directory / "cancel.json", previous / "cancel.json")
                state = store.record(
                    "operation_resumed",
                    status="planned",
                    attempt=attempt,
                    owner=None,
                    owner_token=uuid.uuid4().hex,
                    result=None,
                    error=None,
                    progress={"completed": 0, "total": len(request.commands)},
                )
                try:
                    with os.fdopen(
                        private_open(directory / "worker.log", append=True), "wb"
                    ) as log:
                        child = subprocess.Popen(
                            [
                                sys.executable,
                                "-m",
                                "pg_maker.execution.worker",
                                "--directory",
                                str(directory),
                                "--owner-fd",
                                str(owner_fd),
                            ],
                            stdin=subprocess.DEVNULL,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                            pass_fds=(owner_fd,),
                        )
                    self._children[directory.name] = child
                    return store.record("worker_started", owner=process_identity(child.pid))
                except OSError as exc:
                    error = PreconditionError("cannot start resumed worker")
                    store.record("worker_start_failed", status="failed", error=error.as_dict())
                    raise error from exc

    def events(
        self, directory: Path, *, after_sequence: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        directory = self._reference(directory)
        with exclusive_lock(directory / "control.lock"):
            events, last, _ = read_events(
                directory / "events.jsonl", after_sequence=after_sequence, limit=limit
            )
        cursor = events[-1]["sequence"] if events else after_sequence
        return {
            "events": events,
            "last_sequence": cursor,
            "has_more": last is not None and last["sequence"] > cursor,
        }

    def cancel(self, directory: Path) -> dict[str, Any]:
        directory = self._reference(directory)
        with exclusive_lock(directory / "control.lock"):
            state = OperationStore(directory).load()
            if state["status"] not in TERMINAL_STATUSES:
                write_json(directory / "cancel.json", {"requested_at": utc_now()})
        state = self.status(directory)
        if state["status"] == "interrupted" or (
            state["status"] in TERMINAL_STATUSES and state["resources"]
        ):
            # A lost worker cannot consume the request. Clean only its recorded exact resources.
            with exclusive_lock(directory / "owner.lock", blocking=False):
                with exclusive_lock(directory / "control.lock"):
                    store = OperationStore(directory)
                    state = store.load(repair=True)
                    if state["resources"]:
                        payload = read_json(directory / "input.json")
                        if content_hash(payload) != state["input_hash"]:
                            raise PreconditionError("operation input hash changed")
                        request = parse_request(payload)
                        runner = DockerRunner(
                            directory,
                            operation_id=state["operation_id"],
                            owner_token=state["owner_token"],
                        )
                        store.record("cleanup_requested")
                        for resource in state["resources"]:
                            if resource["kind"] == "process":
                                stop_process_group(resource["identity"], grace_seconds=1)
                            elif resource["kind"] == "docker":
                                environment = (
                                    request.environment
                                    if isinstance(request, OperationRequest)
                                    else EnvironmentSpec(**resource["environment"])
                                )
                                handle = runner.intended_handle(environment)
                                from dataclasses import replace

                                handle = replace(handle, container_id=resource.get("container_id"))
                                if (
                                    handle.name != resource["name"]
                                    or handle.labels != resource["labels"]
                                ):
                                    raise OwnershipError(
                                        "recorded resource does not match operation owner"
                                    )
                                runner.release(handle, CleanupPolicy.REMOVE)
                        state = store.record("cleanup_finished", resources=[])
        return state

    def config_show(self, manifest: Path, **options) -> dict:
        from pg_maker.config import load_config

        return load_config(manifest, **options).to_document()

    def export(self, directory: Path, destination: Path, *, include_inputs=False) -> dict:
        from pg_maker.artifacts.manifest import export_operation

        directory = self._reference(directory)
        state = self.status(directory)
        if state["status"] not in TERMINAL_STATUSES:
            raise PreconditionError("export requires a finished operation")
        with exclusive_lock(directory / "owner.lock", blocking=False):
            return export_operation(
                directory, destination.absolute(), include_inputs=include_inputs
            )

    def validate(self, manifest: Path, **options) -> dict:
        from pg_maker.config import environment_requirements, load_config

        config = load_config(manifest, **options)
        requirements = environment_requirements(config.data)
        return {
            "valid": True,
            "variants": [v["id"] for v in config.data["variants"]],
            "environment_requirements_hash": content_hash(requirements),
        }

    def prepare(
        self,
        manifest: Path,
        *,
        output: Path,
        input_store: Path,
        environments: Path | None = None,
        offline: bool = False,
        request_id: str | None = None,
        operation_id: str | None = None,
        **options,
    ) -> dict:
        from pg_maker.config import environment_requirements, load_config
        from pg_maker.planning.documents import verify

        config = load_config(manifest, **options)
        requirements = environment_requirements(config.data)
        env = read_json(environments) if environments else None
        if env is not None:
            verify(env, "environment")
            if env["spec"]["requirements_hash"] != content_hash(requirements):
                raise PreconditionError("prepared environments are stale; run env prepare")
        request = PreparationRequest(
            "resolve_inputs" if env is not None else "prepare_environments",
            config.data,
            config.bindings,
            str(output.absolute()),
            str(input_store.absolute()),
            env,
            offline,
        )
        return self.start_operation(request, request_id=request_id, operation_id=operation_id)

    def prepare_environments(
        self, manifest: Path, *, output: Path, input_store: Path, **options
    ) -> dict:
        return self.prepare(manifest, output=output, input_store=input_store, **options)

    def resolve(
        self, manifest: Path, *, environments: Path, output: Path, input_store: Path, **options
    ) -> dict:
        return self.prepare(
            manifest, environments=environments, output=output, input_store=input_store, **options
        )

    def plan(
        self,
        lock: Path,
        *,
        goal: str = "build",
        input_store: Path | None = None,
        run_db_tests: bool | None = None,
    ) -> dict:
        from pg_maker.planning.plan import build_plan

        result = build_plan(
            read_json(lock), goal=goal, input_store=input_store, run_db_tests=run_db_tests
        )
        result["transport"]["lock"] = str(lock.absolute())
        return result

    def wait(self, directory: Path, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        if timeout_seconds is not None and (
            type(timeout_seconds) is not int or timeout_seconds <= 0
        ):
            raise ValidationError("timeout_seconds must be a positive integer")
        start = time.monotonic()
        while True:
            state = self.status(directory)
            if state["status"] in TERMINAL_STATUSES:
                return state
            if timeout_seconds is not None and time.monotonic() - start >= timeout_seconds:
                raise PreconditionError("waiting timed out; operation is still running")
            time.sleep(0.1)

    def build(
        self,
        lock: Path,
        *,
        goal: str | None = None,
        plan: Path | None = None,
        plan_hash: str | None = None,
        run_db_tests: bool | None = None,
        input_store: Path | None = None,
        cache_store: Path | None = None,
        use_cache: bool = True,
        operation_id: str | None = None,
        request_id: str | None = None,
        with_doc: bool | None = None,
        doc_languages: list[str] | None = None,
        features: list[str] | None = None,
        build_mode: str | None = None,
        components: list[str] | None = None,
        reuse_from: Path | None = None,
    ) -> dict:
        from pg_maker.builders.autotools import validate_build
        from pg_maker.planning.documents import verify
        from pg_maker.planning.plan import build_plan

        locked = read_json(lock)
        if locked.get("kind") == "BuildPlan":
            if plan is not None:
                raise ValidationError("supply either a positional plan or --plan")
            plan = lock
            reference = locked.get("transport", {}).get("lock")
            if not reference:
                raise PreconditionError("plan has no lock transport reference")
            locked = read_json(Path(reference))
        verify(locked, "lock")
        config = locked["spec"]["config"]
        if doc_languages is not None:
            if not config["documentation"]["enabled"]:
                raise ValidationError("doc-language requires enabled documentation")
            aliases = config["documentation"].get("language_aliases", {})
            doc_languages = [aliases.get(value, value) for value in doc_languages]
        assertions = (
            (with_doc, config["documentation"]["enabled"], "with-doc"),
            (doc_languages, config["documentation"]["languages"], "doc-language"),
            (features, [f["metadata"]["id"] for f in config["features"]], "feature"),
            (build_mode, config["defaults"]["build"]["mode"], "build-mode"),
        )
        for expected_value, actual, name in assertions:
            if expected_value is not None and expected_value != actual:
                raise PreconditionError(name + " differs from locked configuration; resolve again")
        supplied = None
        if plan is not None:
            supplied = read_json(plan)
            verify(supplied, "plan")
            if goal is None:
                goal = supplied["spec"]["goal"]
            if run_db_tests is None:
                run_db_tests = supplied["spec"]["run_db_tests"]
            if components is None:
                components = supplied["spec"].get("components")
        goal = goal or "build"
        expected = build_plan(
            locked,
            goal=goal,
            input_store=input_store,
            run_db_tests=run_db_tests,
            components=components,
        )
        validate_build(
            locked["spec"]["config"], goal, run_db_tests=expected["spec"]["run_db_tests"]
        )
        if goal == "verified" and any(
            not image.get("runtime_repository")
            for image in locked["spec"]["environments"]["spec"]["images"]
            if image["role"] == "build"
        ):
            raise PreconditionError("verified goal requires a prepared offline runtime repository")
        # Regenerating a plan must not change an idempotent build request's input hash.
        expected["created_at"] = locked["created_at"]
        if supplied is not None:
            if supplied["plan_hash"] != expected["plan_hash"]:
                raise PreconditionError("supplied plan differs from the required build graph")
        if plan_hash is not None and plan_hash != expected["plan_hash"]:
            raise PreconditionError("plan hash does not match")
        request = BuildRequest(
            locked,
            expected,
            expected["transport"]["input_store"],
            str((cache_store or self.directory / ".cache").absolute()) if use_cache else None,
            reuse_from=str(reuse_from.absolute()) if reuse_from else None,
        )
        return self.start_operation(request, operation_id=operation_id, request_id=request_id)
