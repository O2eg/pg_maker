# MakerService and CLI

`api.MakerService(operations_directory: Path)` takes an absolute directory. The
Python API does not read argparse state or change the working directory.

| Method | Input | Result or effect |
| --- | --- | --- |
| doctor | None | Local Linux Docker version/architecture; unavailable daemon is a precondition failure |
| start_operation | Typed request, optional operation/request IDs | Persist input, start worker, return OperationState |
| status | Absolute operation directory | Read journal state; reconstruct snapshot when the owner is gone |
| events | Directory, nonnegative cursor, limit 1–1000 | Events, last sequence and has-more flag |
| cancel | Existing operation | Persist cancellation; clean verified orphan resources |
| wait | Directory, optional positive timeout | Terminal state; waiting timeout does not cancel the worker |
| config_show / validate | Manifest and overrides | Effective config/provenance/bindings or validated variants/requirements |
| prepare_environments / resolve | Manifest and operation options | Durable operation producing EnvironmentManifest or BuildLock |
| plan | Lock, goal, input store and test override | BuildPlan without downloads, Docker or compilation |
| build | Lock or saved plan and execution options | Durable build operation |
| resume / export / artifacts | Operation/result references | Verified retry or portable output references |
| dev_open / dev_status / dev_patch | Session and patch inputs | Owned Git session and patch stack operations |
| dev_build / dev_explain / dev_execute | Session and iteration/selection options | New iteration, reuse reasons or durable debug operation |
| dev_pin / dev_prune | Session, iteration or retention plan | Preserve checkpoints or clean old scratch |

```python
from pathlib import Path
from pg_maker.api import MakerService
from pg_maker.models import CommandSpec, EnvironmentSpec, OperationRequest
from pg_maker.runners.docker import current_user

service = MakerService(Path("/absolute/project/operations"))
request = OperationRequest(
    EnvironmentSpec("sha256:" + "0" * 64, current_user()),  # Use a real local image ID.
    (CommandSpec("kernel", ("uname", "-s"), timeout_seconds=30),),
)
state = service.start_operation(request, request_id="check-1")
state = service.wait(service.directory / state["operation_id"])
```

Repeated request IDs with identical input hashes return the existing operation,
including terminal operations. Different inputs produce `precondition_failed`;
occupied operation IDs produce `ownership_error`. The request registry is locked.

Preparation/resolve use the same worker, journal, cancellation and idempotency
machinery. Inputs contain effective configuration and explicit bindings. Successful
results contain document path, file SHA256 and semantic document hash. Their
`--out` is a file; `--operations` selects the operations directory. `plan --out`
writes JSON; otherwise it returns the document.

`build(lock, goal=None, plan=None, plan_hash=None, run_db_tests=None,
input_store=None, cache_store=None, use_cache=True, operation_id=None,
request_id=None, components=None, reuse_from=None)` validates and starts BuildRequest.
A supplied plan determines the default goal; otherwise it is build. The plan must
match the reconstructed DAG and requested plan hash. Plan
creation timestamps do not change request idempotency. The plan or lock controls
the default test mode; explicit overrides must agree with a supplied plan. Changing
that mode changes plan/input identity.

Goals include source/build/package/verified and doc-source/translations/
translated-docs/docs. Documentation commands use the same build service. Build-time
feature/mode/documentation flags validate the lock instead of modifying it. Resolve
again to change the locked configuration; development sessions create new locks for
code edits. Results record reports, hashes, qualification, database tests, failures
and publication eligibility. Only full successful qualification can grant eligibility.

`runner check` exercises infrastructure commands using an existing immutable image;
it is not a PostgreSQL build. `--detach` returns after worker startup; otherwise the
CLI waits. `--machine` always uses the same envelope, including errors.

Exit codes: invalid CLI/input 2, failed precondition 3, unsupported 4, partial 5,
execution failure 6, cancellation 7, ownership 8. Status/events/cancel report the
success of reading/requesting; operation status is in the result. SIGINT in a waiting
CLI returns 130 without stopping the worker.

New operation types require an input schema and explicit engine dispatch. New CLI
commands call the same service. See [recovery](recovery.md), [documentation](documentation.md)
and [development](development.md) for operation-specific contracts.
