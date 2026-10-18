"""Local Docker CLI adapter. No pulls, shell recipes or host build execution."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from pg_maker.documents import relative_output
from pg_maker.errors import (
    CommandError,
    OwnershipError,
    PreconditionError,
    UnsupportedError,
    ValidationError,
)
from pg_maker.execution.process import run_process
from pg_maker.models import (
    ArtifactRef,
    ArtifactSet,
    CancellationResult,
    CleanupPolicy,
    CommandResult,
    CommandSpec,
    EnvironmentHandle,
    EnvironmentSpec,
    OutputSpec,
)
from pg_maker.security import resolve_secrets
from pg_maker.storage import ensure_directory, file_hash, parse_json, sync_directory


class DockerRunner:
    def __init__(
        self,
        directory: Path,
        *,
        operation_id: str,
        owner_token: str,
        cancelled: Callable[[], bool] = lambda: False,
        on_process: Callable[[dict], None] | None = None,
        attempt: int = 1,
        workspace: Path | None = None,
    ) -> None:
        self.directory, self.operation_id, self.owner_token = directory, operation_id, owner_token
        self.cancelled, self.on_process = cancelled, on_process
        self.logs = directory / "attempts" / str(attempt) / "logs"
        self.workspace = workspace or directory / "workspace"
        if not self.workspace.is_relative_to(directory):
            raise ValidationError("runner workspace must belong to its operation")

    def intended_handle(self, environment: EnvironmentSpec) -> EnvironmentHandle:
        return EnvironmentHandle(
            name=f"pg-maker-{self.owner_token}",
            labels={
                "io.pg-maker.owner": self.owner_token,
                "io.pg-maker.run_id": self.operation_id,
                "io.pg-maker.target_id": environment.target_id,
            },
            workspace=self.workspace,
            environment=environment,
        )

    def _control(self, args: list[str], *, check: bool = True, timeout: int = 30) -> CommandResult:
        result = run_process(
            ["docker", *args],
            log_directory=self.logs,
            name="docker-" + uuid.uuid4().hex,
            timeout_seconds=timeout,
            on_start=self.on_process,
        )
        if check and not result.succeeded:
            raise PreconditionError(
                "Docker command failed",
                details={
                    "action": args[0],
                    "returncode": result.returncode,
                    "stderr": result.stderr_tail,
                    "timed_out": result.timed_out,
                },
            )
        return result

    def doctor(self) -> dict[str, Any]:
        self._require_local_daemon()
        result = self._control(["version", "--format", "{{json .Server}}"])
        server = parse_json(result.stdout_tail)
        info = parse_json(
            self._control(
                [
                    "info",
                    "--format",
                    '{"OSType":{{json .OSType}},"Architecture":{{json .Architecture}}}',
                ]
            ).stdout_tail
        )
        if not server or info.get("OSType") != "linux":
            raise PreconditionError("a Linux Docker daemon is required")
        return {
            "available": True,
            "server_version": server["Version"],
            "os": info["OSType"],
            "architecture": info["Architecture"],
        }

    def _require_local_daemon(self) -> None:
        if os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
            endpoint = os.environ["DOCKER_HOST"]
        else:
            context = self._control(["context", "show"]).stdout_tail.strip()
            endpoint = self._control(
                [
                    "context",
                    "inspect",
                    context,
                    "--format",
                    "{{.Endpoints.docker.Host}}",
                ]
            ).stdout_tail.strip()
        if not endpoint.startswith("unix://"):
            raise UnsupportedError("only a local Unix-socket Docker daemon is implemented")

    def _inspect(self, handle: EnvironmentHandle) -> dict[str, Any] | None:
        result = self._control(
            [
                "container",
                "inspect",
                "--format",
                '{"id":{{json .Id}},"labels":{{json .Config.Labels}},'
                '"running":{{json .State.Running}}}',
                handle.container_id or handle.name,
            ],
            check=False,
        )
        if not result.succeeded:
            # A failed daemon/permission/connection check must not be mistaken for absence.
            if "No such container" in result.stderr_tail or "No such object" in result.stderr_tail:
                return None
            raise PreconditionError(
                "cannot inspect Docker resource",
                details={
                    "stderr": result.stderr_tail,
                },
            )
        data = parse_json(result.stdout_tail)
        if any(
            (data.get("labels") or {}).get(key) != value for key, value in handle.labels.items()
        ):
            raise OwnershipError("container ownership labels do not match")
        if handle.container_id and data["id"] != handle.container_id:
            raise OwnershipError("container identity changed")
        return data

    def prepare(
        self,
        environment: EnvironmentSpec,
        *,
        memory_bytes: int | None = None,
        cpu_jobs: int | None = None,
        prefix_mount: tuple[Path, str] | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> EnvironmentHandle:
        self._require_local_daemon()
        handle = self.intended_handle(environment)
        allowed = {"CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID", "KILL"}
        if set(capabilities) - allowed or (capabilities and environment.user != "0:0"):
            raise ValidationError("unsupported container capabilities")
        self._control(["image", "inspect", environment.image, "--format", "{{.Id}}"])
        if self._inspect(handle) is not None:
            raise OwnershipError("operation container already exists; reconcile it before reuse")
        for part in ("source", "build", "output"):
            ensure_directory(handle.workspace / part)
        command = [
            "create",
            "--pull",
            "never",
            "--name",
            handle.name,
            "--init",
            "--network",
            "none",
            "--user",
            environment.user,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--workdir",
            "/work/build",
            "--entrypoint",
            "sleep",
        ]
        for key, value in handle.labels.items():
            command += ["--label", f"{key}={value}"]
        for capability in capabilities:
            command += ["--cap-add", capability]
        if memory_bytes is not None:
            command += [
                "--memory",
                str(memory_bytes),
                "--memory-swap",
                str(memory_bytes),
                "--shm-size",
                str(min(memory_bytes // 4, 1073741824)),
            ]
        if cpu_jobs is not None:
            command += ["--cpus", str(cpu_jobs)]
        if prefix_mount is not None:
            source, destination = prefix_mount
            if not source.is_relative_to(handle.workspace) or "," in str(source):
                raise ValidationError("prefix mount must be inside operation workspace")
            if not destination.startswith("/opt/") or any(c in destination for c in ",\n\r"):
                raise ValidationError("build prefix mount must be below /opt")
            ensure_directory(source)
            command += ["--mount", f"type=bind,src={source},dst={destination}"]
        for part in ("source", "build", "output"):
            source = str(handle.workspace / part)
            if "," in source:
                raise ValidationError("Docker bind path must not contain commas")
            mount = f"type=bind,src={source},dst=/work/{part}"
            command += ["--mount", mount + (",readonly" if part == "source" else "")]
        command += [environment.image, "infinity"]
        result = self._control(command)
        handle = replace(handle, container_id=result.stdout_tail.strip())
        self._inspect(handle)
        self._control(["start", handle.container_id])
        return handle

    def execute(self, handle: EnvironmentHandle, command: CommandSpec) -> CommandResult:
        data = self._inspect(handle)
        if data is None or not data["running"]:
            raise PreconditionError("execution container is not running")
        secret_values = resolve_secrets(command.secret_env)
        environment = {**command.env, **secret_values}
        args = ["docker", "exec", "--user", handle.environment.user, "--workdir", command.cwd]
        if any("\n" in value or "\r" in value for value in environment.values()):
            raise ValidationError("container environment values must be single-line strings")
        # An anonymous memory file keeps values off argv and off disk. It also prevents a
        # container PATH/DOCKER_HOST/PYTHONPATH from changing the controller's own environment.
        descriptor = os.memfd_create("pg-maker-container-env", os.MFD_CLOEXEC)
        try:
            with os.fdopen(os.dup(descriptor), "w", encoding="utf-8") as stream:
                for key, value in sorted(environment.items()):
                    stream.write(f"{key}={value}\n")
                stream.flush()
            os.lseek(descriptor, 0, os.SEEK_SET)
            args += ["--env-file", f"/proc/self/fd/{descriptor}", data["id"], *command.argv]
            result = run_process(
                args,
                log_directory=self.logs,
                name=command.id,
                timeout_seconds=command.timeout_seconds,
                cancelled=self.cancelled,
                secrets=tuple(secret_values.values()),
                on_start=self.on_process,
                pass_fds=(descriptor,),
            )
        finally:
            os.close(descriptor)
        if result.cancelled or result.timed_out:
            self.cancel(handle)
        return result

    def collect(self, handle: EnvironmentHandle, outputs: OutputSpec) -> ArtifactSet:
        root = handle.workspace / "output"
        artifacts = []
        for path in outputs.paths:
            if not relative_output(path):
                raise ValidationError("invalid relative output path")
            target = root / path
            if any(p.is_symlink() for p in (target, *target.parents)):
                raise OwnershipError("output must not traverse a symbolic link")
            if not target.is_file():
                raise CommandError("required output is missing", details={"path": path})
            with target.open("rb") as stream:
                os.fsync(stream.fileno())
            sync_directory(target.parent)
            artifacts.append(
                ArtifactRef(
                    str(target.relative_to(self.directory)),
                    file_hash(target),
                    target.stat().st_size,
                )
            )
        return ArtifactSet(tuple(artifacts))

    def cancel(self, handle: EnvironmentHandle) -> CancellationResult:
        data = self._inspect(handle)
        if data is None or not data["running"]:
            return CancellationResult(False)
        self._control(["stop", "--time", "2", data["id"]], timeout=10)
        after = self._inspect(handle)
        if after and after["running"]:
            raise CommandError("container remained running after cancellation")
        return CancellationResult(True)

    def release(self, handle: EnvironmentHandle, policy: CleanupPolicy) -> None:
        data = self._inspect(handle)
        if data is None:
            return
        self.cancel(handle)
        if policy == CleanupPolicy.REMOVE:
            # Base images may declare VOLUME. Remove the container's anonymous
            # volumes as well; Docker preserves explicitly named volumes here.
            self._control(["rm", "--volumes", data["id"]])


def current_user() -> str:
    return f"{os.getuid()}:{os.getgid()}"
