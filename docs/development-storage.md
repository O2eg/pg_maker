# Disk use during development

Check capacity before a build and after a series of debugging runs:

```bash
df -h /tmp .
du -sh /tmp/pg-maker-*
docker system df
docker ps -a
```

Storage grows independently in InputStore, per-feature/language source/object/prefix
workspaces, documentation output, recovery checkpoints, shared caches, previous
attempt archives, exported results and Docker images/build cache. `--no-cache`
disables shared reuse, but keeps private checkpoints for resume.

`execution.limits.min_free_disk_bytes` is a free-space reserve checked before steps
and checkpoint creation. Development snapshot/copy paths also estimate their copy
size. This is a preflight guard, not a filesystem quota: a compiler or renderer can
consume additional space within a step. Reserve enough space for expected working
trees. Prefer a small fixture or documentation-only goal when a full build is
unnecessary. Database tests remain opt-in.

## Completed operations

Check status and owned resources before cleanup. Cancel active operations through
`pg-maker cancel` and wait for resource release. Export outputs first:

```bash
pg-maker export operations/RUN --out exported-result
pg-maker cleanup operations/RUN --export exported-result --out cleanup.json
pg-maker cleanup operations/RUN --apply cleanup.json
```

The verified preview targets only `workspace/build`, `workspace/source` and
checkpoints. Packages, logs and reports remain. Resume is rejected after scratch
cleanup. Removing InputStore or an image ID referenced by a lock also prevents an
offline rebuild. Input stores can be shared through `transport.input_store`; inspect
those references before removing an entire temporary project.

## Development sessions

```bash
pg-maker dev pin development 000003
pg-maker dev prune development --keep-last 2 --out prune.json
pg-maker dev prune development --keep-last 2 --apply prune.json
```

Current and pinned iterations, working Git trees and patch stacks are retained.
Old inactive iteration/debug scratch is removed only after the plan is revalidated.
Locks, reports and input snapshots remain available. Cleaned iterations cannot be
resumed. Use the separate cache preview/apply operation to reclaim unpinned entries:

```bash
pg-maker cache prune development/cache --out cache-prune.json
pg-maker cache prune development/cache --apply cache-prune.json
```

Completed ordinary operations retain cache pins until their lifecycle cleanup
releases them. InputStore garbage collection is not automatic.

## Docker storage

`docker image prune -a`, `docker builder prune` and `docker volume prune` affect the
shared Docker daemon. They are manual administrative operations, never pg_maker
finalizers. Prefer deleting specific unused images after checking saved locks.
pg_maker removes only containers/resources owned by its operations, including their
anonymous volumes; named volumes are retained.

Historical observation: on 2026-10-17, removing completed stage-4 workspaces reduced
that temporary directory from about 12 GB to 40 MB, releasing roughly 11 GB. State,
logs and reports were retained; other projects' Docker images/volumes were untouched.
