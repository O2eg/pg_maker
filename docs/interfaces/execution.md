# Runner and subprocess execution

The implemented backend is local Docker. Runner is a structural protocol:

```python
prepare(environment: EnvironmentSpec) -> EnvironmentHandle
execute(handle: EnvironmentHandle, command: CommandSpec) -> CommandResult
collect(handle: EnvironmentHandle, outputs: OutputSpec) -> ArtifactSet
cancel(handle: EnvironmentHandle) -> CancellationResult
release(handle: EnvironmentHandle, policy: CleanupPolicy) -> None
```

DockerRunner receives an explicit operation directory, operation ID, owner token
and cancellation callback. Prepare uses an existing immutable image, exact ownership
labels, numeric user, `network=none`, `--init` and no-new-privileges. Init reaps orphan
processes after worker loss. Read-only source and separate writable build/output
mounts belong to the operation. The image must contain sleep; runner checks use
uname/id. The runner neither pulls images nor mounts the Docker socket.

Execute verifies labels and container ID, passing argv/env/cwd separately to Docker
exec. Shell strings are not the API. Command failures return CommandResult;
timeout/cancellation stop the container. Collect verifies required files and records
relative path, size and SHA256. Infrastructure output collection rejects symlinks;
InstallTree has a separate symlink-aware inventory.

Release with remove stops/removes only the verified container. Keep-stopped retains
it. Missing resources are safe to retry; foreign labels produce ownership errors.
Cleanup failure prevents succeeded status.

`execution.process.run_process` streams controller-tool stdout/stderr to separate
private files and keeps at most 16,384 Unicode characters of tail per stream. Log
names are never overwritten. Timeout/cancellation send SIGTERM, allow bounded grace,
then SIGKILL the process group. The direct child's PID is retained until descendant
cleanup even if the child exits first. This primitive is not a host build backend.
An unavailable executable is a precondition failure.

Build preparation passes keyword-only memory/cpu limits and an owned prefix mount
below container `/opt`. These configure memory, swap, CPUs, shared memory and private
installation paths. Host `/opt` is untouched. Mutable source copies are below the
build mount; the source mount contains read-only helper inputs.

The runner accepts attempt and an optional workspace inside the operation directory.
Installation validation uses a separate workspace without build prefix/objects.
Capabilities default to none with cap-drop ALL. Root validation may request only
CHOWN, DAC_OVERRIDE, FOWNER, FSETID, SETUID, SETGID and KILL. SYS_ADMIN, privileged
mode, host networking and Docker socket access are not used.

A new runner must implement the same ownership/cancellation semantics and tests.
Docker CLI details should not leak into builder or packager contracts.
