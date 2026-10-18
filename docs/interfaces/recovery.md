# Resume, checkpoints, cache, resources and export

`MakerService.resume(directory, *, plan_hash=None, input_hash=None)` accepts failed,
cancelled, interrupted and partial operations. Build requires the original plan hash;
preparation requires the original input hash; runtime uses its runtime plan hash.
Successful operations need a new run. Arbitrary container commands and development
debug commands are not replayed automatically.

Resume verifies input.json, lock/input/implementation identity and the same DAG.
Under owner/control locks it cleans only recorded old resources, archives cancel/state,
increments attempt, rotates the owner token and starts the worker. A competing owner
gets ownership_error. New attempts write separate logs; history remains intact.
Edited development sources require a new iteration, never a changed resume input.

## Checkpoints and reusable results

`Checkpoints(branch, steps).save(index, complete=False)` captures completed source,
configure, compile, install, component, collection, dependency and package boundaries,
plus documentation preparation and per-language translation/render steps. Results
are verified before snapshot/index/event publication. Restore verifies plan/stage
keys, manifest and bytes/modes/links. Invalid snapshots fall back to earlier boundaries.
Existing scratch is archived under attempts/N/previous; restore uses fresh roots.
A successful branch of a partial matrix can restore completely. In full-test mode,
an unfinished branch restores before the first test and reruns tests.

SnapshotStore provides capture/verify/restore/lookup/pin/prune under flock with SHA256
and atomic writes. Manifests contain relative paths, file bytes/modes/mtimes or link
targets. Restore does not follow links or overwrite existing roots. Special files
are rejected. Shared core snapshots contain source/obj/prefix/output after world-bin
with tests disabled. Restored objects are private copies with preserved timestamps.

Core keys contain compilation inputs, image, flags, paths and implementation identity.
External components have separate install/evidence/workspace snapshots keyed by own
sources, recipes, core and dependencies. A different lock alone does not invalidate
compatible compilation. Exact-run resume checkpoints still require the original plan.
Test results are never reused from the shared compilation cache. Artifact manifests
distinguish current packaging producer, core build producer and component producers.

Cache defaults to operations/.cache; --cache-store selects another store. --no-cache
disables shared reuse, keeping private checkpoints. Entry and pin publish together.
Operation pins conservatively retain referenced data, including interrupted/completed
runs. Prune removes only unreferenced CAS objects. Private checkpoints, InputStore,
Docker images, packages and reports are not removed automatically.

```bash
pg-maker resume operations/RUN --plan-hash sha256:FULL_HASH --detach
pg-maker resume operations/PREP --input-hash sha256:FULL_HASH --detach
pg-maker cache prune operations/.cache --out prune.json
pg-maker cache prune operations/.cache --apply prune.json
pg-maker export operations/RUN --out delivery --include-inputs
```

Apply recomputes the prune plan under lock; new references or changed bytes invalidate
a stale plan. Private checkpoint stores are rejected by cache prune. Global Docker
prune is never invoked. Development iteration retention releases only its selected
old scratch and pins; see [development](development.md).

## Resource limits and outputs

Preflight checks free disk, MemAvailable capped by cgroup limits and available CPUs.
Insufficient resources fail with precondition_failed. Docker receives CPU, memory,
swap and shared-memory limits. Targets execute sequentially, so make jobs are not
multiplied across targets. Maximum memory is a ceiling, not an advance reservation.

`artifacts.finish` creates ArtifactManifest, immutable VerificationRecord, lock/plan
copies, INSTALL.txt and reports. Package/verification/evidence references are relative
to branch output and hash-verified. Command logs are copied alongside outputs,
including reused compilation evidence. Hashes are not signatures; HTML values are
escaped.

Export accepts a terminal run and a new destination. It verifies manifests, copies
output, attempt logs, journal and state, and omits archived working trees/objects.
A temporary destination is fsynced and atomically renamed. Preparation exports also
verify/copy the produced document. ExportManifest inventories the complete delivery.
Include-inputs adds frozen source/component trees and blobs, not Docker images;
offline rebuild still needs matching local image IDs. Historical diagnostic state may
contain host paths, but artifact/evidence references do not depend on them.

CI uses the public CLI through tools/ci_pipeline.py. Push/PR runs utility checks and
small Docker fixtures. Manual workflow dispatch builds PostgreSQL; long database
suites require run_db_tests=true, default false. Failure/cancellation triggers targeted
cancel and diagnostic export. Local script validation and remote workflow success are
separate evidence. See [disk management](../development-storage.md).
