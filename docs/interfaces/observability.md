# Errors, progress and secrets

MakerError exposes stable code, exit_code, message and structured details. Types
include ValidationError, PreconditionError, UnsupportedError, CancelledError,
OwnershipError, StateError and CommandError.

`ProgressReporter(path, operation_id, target_id=None, attempt=1, stream=None)`
provides configure, info/error, advance, close and context-manager support. It writes
to stderr and an append-only 0600 file by default, including UTC, level, operation,
target, step, attempt and progress. Progress logs do not replace durable events.
Losing CLI stdout does not cancel a detached worker.

Commands have separate stdout/stderr files per attempt. Evidence is not rotated or
deleted automatically. Console messages and exception details redact common credential
fields and URL passwords.

`CommandSpec.secret_env` maps container variable names to worker environment variable
names. Input JSON stores references, never values. Values resolve immediately before
execution. Single-line secrets reach Docker through an anonymous memory fd env-file,
not argv or a disk file. Container environment settings do not alter Docker-client
PATH/DOCKER_HOST. Known secrets are redacted across streaming/UTF-8 boundaries.
Public env rejects meaningful password/token/secret/credential/api_key names.

Command arguments and ordinary environment variables must be public inputs. Redaction
does not detect arbitrary encodings, encryption or transformations of a secret.
Artifact producers are responsible for artifact contents; binary outputs are not
rewritten. Private control/log files reduce accidental disclosure; they do not
isolate an untrusted Docker-daemon owner.
