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
| build | Lock, optional saved plan and execution options | Build operation, or publication operation when `publish_config` is set |
| extension_init / extension_prepare | Extension project, target SDK/package inputs | Editable recipes or durable preparation producing ExtensionLock |
| extension_plan / extension_build | ExtensionLock and operation options | Pure ExtensionPlan or extension build; `publish_config` returns its publication operation |
| publication_plan / publish | Source operation/export and receiver configuration | Verified local publication plan or durable upload/verification operation |
| resume / export | Operation/result references | Verified retry or portable output export |
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
request_id=None, with_doc=None, doc_languages=None, features=None, build_mode=None,
components=None, reuse_from=None, publish_config=None)` validates and starts BuildRequest.
A supplied plan determines the default goal; otherwise it is build. The plan must
match the reconstructed DAG and requested plan hash. Plan
creation timestamps do not change request idempotency. The plan or lock controls
the default test mode; explicit overrides must agree with a supplied plan. Changing
that mode changes plan/input identity.

`build` and `extension_build` accept `publish_config=Path(...)`. With this option,
the service starts the build plus an independent publisher and returns the
publisher's OperationState. Wait on that returned operation ID to include upload
and index failures in the CI outcome. Its input/result identifies the source build.
Cancelling the publisher does not cancel the source build. Without this option,
the returned state belongs to the build itself.

`publication_plan(source, config)` accepts an absolute source directory and a
configuration file Path, validates local evidence and returns a plan without
contacting receivers. `publish(source, config, wait_for_source=False,
operation_id=None, request_id=None)` accepts a configuration Path or validated
configuration mapping. Completed-source requests bind package hashes through the
publication plan. Waiting requests seal that plan after the source finishes; a
repeated successful request must still match the current artifacts. See
[publication](publication.md) for receiver settings and recovery.

Goals include source/build/package/verified/test-full and doc-source/translations/
translated-docs/docs. Documentation commands use the same build service. Build-time
feature/mode/documentation flags validate the lock instead of modifying it. Resolve
again to change the locked configuration; development sessions create new locks for
code edits. Results record reports, hashes, qualification, database tests, failures
and publication eligibility. Only full successful qualification can grant that historical eligibility flag. The
[publication API](publication.md) separately gates uploads on successful installed-package
tests and exact hashes, including explicitly selected package-test-only builds.

`pg-maker artifacts DIRECTORY` reads verified output references through
`artifacts.catalog.operation_artifacts(directory, state)`; there is no
`MakerService.artifacts` method. Runtime image and standalone diagnostic commands
similarly use their dedicated adapters with the shared operation service.

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
