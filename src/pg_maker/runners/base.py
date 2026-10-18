from pathlib import Path
from typing import Protocol

from pg_maker.models import (
    ArtifactSet,
    CancellationResult,
    CleanupPolicy,
    CommandResult,
    CommandSpec,
    EnvironmentHandle,
    EnvironmentSpec,
    OutputSpec,
)


class Runner(Protocol):
    def prepare(
        self,
        environment: EnvironmentSpec,
        *,
        memory_bytes: int | None = None,
        cpu_jobs: int | None = None,
        prefix_mount: tuple[Path, str] | None = None,
        capabilities: tuple[str, ...] = (),
    ) -> EnvironmentHandle: ...
    def execute(self, handle: EnvironmentHandle, command: CommandSpec) -> CommandResult: ...
    def collect(self, handle: EnvironmentHandle, outputs: OutputSpec) -> ArtifactSet: ...
    def cancel(self, handle: EnvironmentHandle) -> CancellationResult: ...
    def release(self, handle: EnvironmentHandle, policy: CleanupPolicy) -> None: ...
