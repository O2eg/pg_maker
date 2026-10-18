"""Small public data contracts. No CLI namespaces or Docker client types leak here."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class OperationStatus(str, Enum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    PARTIAL = "partial"


TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "interrupted", "partial"})


@dataclass(frozen=True)
class CommandSpec:
    id: str
    argv: tuple[str, ...]
    cwd: str = "/work/build"
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 3600


@dataclass(frozen=True)
class EnvironmentSpec:
    image: str
    user: str
    target_id: str = "infrastructure"


@dataclass(frozen=True)
class OperationRequest:
    environment: EnvironmentSpec
    commands: tuple[CommandSpec, ...]
    outputs: tuple[str, ...] = ()
    operation_kind: str = "container_commands"

    def to_document(self) -> dict[str, Any]:
        from pg_maker.storage import canonical_json, parse_json

        return parse_json(
            canonical_json(
                {
                    "api_version": "pg_maker/operation-input/v1",
                    "kind": "OperationInput",
                    **asdict(self),
                }
            )
        )

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> OperationRequest:
        from pg_maker.documents import validate_document

        validate_document(document, "operation-input")
        return cls(
            environment=EnvironmentSpec(**document["environment"]),
            commands=tuple(
                CommandSpec(**{**cmd, "argv": tuple(cmd["argv"])}) for cmd in document["commands"]
            ),
            outputs=tuple(document["outputs"]),
            operation_kind=document["operation_kind"],
        )


@dataclass(frozen=True)
class PreparationRequest:
    operation_kind: str
    config: dict
    bindings: dict[str, str]
    output: str
    input_store: str
    environments: dict | None = None
    offline: bool = False

    @property
    def commands(self) -> tuple:
        # The operation progress is refined when its worker starts.
        return (self.operation_kind,)

    def to_document(self) -> dict:
        return {
            "api_version": "pg_maker/operation-input/v1",
            "kind": "OperationInput",
            **asdict(self),
        }


@dataclass(frozen=True)
class BuildRequest:
    lock: dict
    plan: dict
    input_store: str
    cache_store: str | None = None
    operation_kind: str = "build"
    reuse_from: str | None = None

    @property
    def commands(self) -> tuple:
        return tuple(self.plan["spec"]["steps"])

    def to_document(self) -> dict:
        return {
            "api_version": "pg_maker/operation-input/v1",
            "kind": "OperationInput",
            **asdict(self),
        }


@dataclass(frozen=True)
class RuntimeRequest:
    plan: dict
    output: str
    operation_kind: str = "runtime_image"

    @property
    def commands(self) -> tuple:
        return ("runtime_image",)

    @property
    def offline(self) -> bool:
        return True

    @property
    def config(self) -> dict:
        return {
            "defaults": {
                "execution": {
                    "limits": {
                        "min_free_disk_bytes": self.plan["profile"]["min_free_disk_bytes"],
                        "stage_timeout_seconds": 1800,
                    }
                }
            }
        }

    def to_document(self) -> dict:
        return {
            "api_version": "pg_maker/operation-input/v1",
            "kind": "OperationInput",
            **asdict(self),
        }


@dataclass(frozen=True)
class DevelopmentCommandRequest:
    source_run: str
    source_input_hash: str
    step: dict
    argv: tuple[str, ...]
    cwd: str
    timeout_seconds: int = 3600
    operation_kind: str = "development_command"

    @property
    def commands(self):
        return (self.argv,)

    def to_document(self):
        return {
            "api_version": "pg_maker/operation-input/v1",
            "kind": "OperationInput",
            **asdict(self),
            "argv": list(self.argv),
        }


def parse_request(
    document: dict,
) -> OperationRequest | PreparationRequest | BuildRequest | RuntimeRequest:
    from pg_maker.documents import validate_document

    validate_document(document, "operation-input")
    if document["operation_kind"] == "container_commands":
        return OperationRequest.from_document(document)
    request_type = {
        "build": BuildRequest,
        "runtime_image": RuntimeRequest,
        "development_command": DevelopmentCommandRequest,
    }.get(document["operation_kind"], PreparationRequest)
    return request_type(
        **{key: value for key, value in document.items() if key not in {"api_version", "kind"}}
    )


@dataclass(frozen=True)
class EnvironmentHandle:
    name: str
    labels: dict[str, str]
    workspace: Path
    environment: EnvironmentSpec
    container_id: str | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout_log: str
    stderr_log: str
    stdout_tail: str
    stderr_tail: str
    elapsed_ms: int
    timed_out: bool = False
    cancelled: bool = False

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


@dataclass(frozen=True)
class OutputSpec:
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ArtifactSet:
    artifacts: tuple[ArtifactRef, ...] = ()


@dataclass(frozen=True)
class CancellationResult:
    stopped: bool


class CleanupPolicy(str, Enum):
    REMOVE = "remove"
    KEEP_STOPPED = "keep_stopped"
