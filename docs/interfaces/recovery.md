# Resume, checkpoints, cache, resources and export

`MakerService.resume(directory, *, plan_hash=None, input_hash=None)` accepts failed,
cancelled, interrupted and partial operations. The required hash depends on the
operation kind:

| Operation | Resume argument | Hash source |
| --- | --- | --- |
| Database/documentation build, including development build iterations | `--plan-hash` | `plan.plan_hash` in the original operation's `input.json` |
| Runtime image build | `--plan-hash` | Original runtime plan hash |
| Environment preparation and source resolution | `--input-hash` | `input_hash` in the operation state |
| Standalone extension preparation/build | `--input-hash` | `input_hash` in the operation state |
| Publication | `--input-hash` | `input_hash` in the publication operation state |

The input hash is not the BuildLock/ExtensionLock hash. `status --machine` exposes
it in `result.input_hash`. Successful operations need a new run. Arbitrary container
commands and development debug commands are not replayed automatically.

Resume verifies input.json and the operation-specific identity: build locks and DAGs,
extension SDK locks, runtime plans or sealed publication plans and source artifacts.
Under owner/control locks it cleans only recorded old resources, archives cancel/state,
increments attempt, rotates the owner token and starts the worker. A competing owner
gets ownership_error. New attempts write separate logs; history remains intact.
Edited development sources require a new iteration, never a changed resume input.

## Checkpoints and reusable results

Normal recovery uses `workspace-journal.json`: compilation keys, stage statuses,
producer provenance and installed-file hashes. Core/contrib and each external
component have separate build directories. Exact-run resume preserves those trees;
`build NEW_LOCK --reuse-from OLD_RUN` acquires the old owner lock and renames its
workspace into the new run without copying objects. Old inputs and evidence stay
immutable. Source-only extension edits synchronize changed/removed sources with
fresh timestamps. Recipe, dependency, image or compiler changes invalidate the
appropriate component; core changes invalidate dependents conservatively.

Only matching successful compilation steps are skipped. Tests, collection,
dependency analysis and packaging run again. Ordinary make failures retain partial
objects; interrupted/ambiguous components are cleaned before retry. Installation
uses isolated DESTDIR directories and durable per-component ownership records.
Partial staging never enters the shared prefix; interrupted publication is replayed
before work resumes. Removed component files are retired. Before retrying a suite,
old test evidence moves to `attempts/N/previous-evidence`; only fresh output counts
as the new result.

`execution.checkpoints` selects `final` (default), `milestones` or `all`. Final
snapshots hold prefix and output/evidence. Milestones additionally save the completed
core/contrib boundary. All saves each eligible compile/install/package boundary.
Intermediate snapshots include core source/objects and install/journal metadata,
not external component build trees or Rust toolchains. Intermediate recovery uses
the restored journal and reruns every test; legacy snapshots without a journal
restore before the first test. A fully completed exact-plan checkpoint can finish interrupted operation finalization with its verified evidence.

SnapshotStore verifies plan/stage keys, SHA256, modes and links before restoring into
fresh roots under flock. Invalid checkpoints fall back to earlier ones. Normal
workspace recovery does not copy or archive the tree; snapshot fallback may archive
invalid scratch. Successful package/verified operations remove compiler scratch and
archived build trees and validation scratch by default (`workspace_retention: on-failure`). Failed or
build-only runs keep it; `keep` also retains successful package workspaces.

Prepared image records are keyed by base image ID, target architecture, selected
packages/tools/locales, builder user and preparation adapter. Reuse verifies the
record hash and that Docker still has the exact image ID. Changing requested inputs
or removing that image forces preparation. Mutable apt repositories are not polled
on a cache hit; update the base/input recipe to request a new environment.

Rust/pgrx toolchains and vendored crates are shared under `InputStore/toolchains`.
Keys contain pinned extra-source hashes (including compiler/stdlib architecture),
build image, PostgreSQL/build settings, prefix and helper identity. A lock serializes
creation/use; file hashes and modes are checked before reuse. Corruption rebuilds
the cache. Cargo build artifacts remain in the component workspace and are cleaned
with it. Immutable input digests are memoized within a process by device, inode,
size, nanosecond mtime/ctime and mode; subsequent checks still walk metadata and
rehash changed entries. Same-size/mtime-restored edits are detected by ctime.

The legacy shared SnapshotStore remains available for old runs and explicit prune.
`--no-cache` disables that shared snapshot cache, not explicit workspace reuse or
input/toolchain/image caches. New workspace recovery does not publish full component
object trees into it. Ordinary `cleanup` does not release legacy shared-cache pins;
development pruning releases pins for selected iterations. See [cleanup](cleanup.md).
InputStore and Docker garbage collection remain explicit; packages/reports are
never automatically deleted. Artifact manifests distinguish current packaging
producer from core/component compilation producers.

```bash
pg-maker resume operations/RUN --plan-hash sha256:FULL_HASH --detach
pg-maker resume operations/PREP --input-hash sha256:FULL_INPUT_HASH --detach
pg-maker resume extensions18/operations/build --input-hash sha256:FULL_INPUT_HASH --detach
pg-maker resume operations/PUBLICATION --input-hash sha256:FULL_INPUT_HASH --detach
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

The workspace/checkpoint details above describe database builds. Standalone
extensions retain per-component receipts and SDK inputs as described in
[their contract](extensions.md#dependencies-and-qualification). Publication resumes
by checking the same tested artifacts and reconciling receiver state, including
previous uploads; it does not rebuild PostgreSQL. If its source build failed,
resume that build first, then its publisher. See [publication recovery](publication.md#gate-integrity-and-recovery).

CI uses the public CLI through tools/ci_pipeline.py. Push/PR runs utility checks and
small Docker fixtures. Manual workflow dispatch builds PostgreSQL with standard
database suites by default; expensive core suites require test-full or a locked
`test_suites: [standard, long]` policy. Failure/cancellation triggers targeted
cancel and diagnostic export. Local script validation and remote workflow success are
separate evidence. See [disk management](../development-storage.md).
