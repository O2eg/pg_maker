# Developing PostgreSQL and extensions

Start with prepared environments, a BuildLock and retained InputStore. Git commands
operate inside repositories owned by the session. `--repository` copies a local
PostgreSQL repository with history; the original checkout stays with its owner.

```bash
pg-maker dev open work --lock example/lock.json
# Alternative: --from-run /path/to/operations/RUN
# Preserve local history with --repository /path/to/repository.
pg-maker dev status work
```

Status returns `trees.postgresql.source` and paths such as
`trees.component:pg-wait-sampling.source`. Edit these with an ordinary editor.
Bundled contrib lives under PostgreSQL's `contrib`; external components have their
own Git trees. Recipe ID `pg-wait-sampling` differs from SQL name `pg_wait_sampling`.

```bash
pg-maker dev patch work commit --scope component:pg-wait-sampling --id fix-build
pg-maker dev explain work --component pg-wait-sampling
pg-maker dev build work --component pg-wait-sampling --detach
pg-maker dev status work
# After the iteration has stopped:
pg-maker dev exec work --component pg-wait-sampling -- make -n
pg-maker dev build work --goal package --detach
```

Build snapshots uncommitted edits too. Editing after startup cannot change that
iteration's immutable inputs. Package/docs goals use the complete feature;
documentation must be enabled in the initial lock. Debug execution uses a separate
copy of the selected iteration's working results. The default is the latest iteration;
`--iteration 000001` selects another and `--target` selects a platform.

Use ordinary status/events/cancel commands with the returned operation_directory.
Build iterations support ordinary resume; debug commands are repeated as new runs.
A corrected source tree requires a new `dev build`. Resume retries the original
operation with its original inputs.

## Patch series

```bash
pg-maker dev patch work import --patch /path/to/series.json
# A single patch: --patch /path/to/fix.patch --id fix-name
pg-maker dev patch work pop --count 2
# Edit the current patch; refresh replays the remaining series after it.
pg-maker dev patch work refresh
pg-maker dev patch work rebase --onto-lock /path/to/new-version-lock.json
# Resolve conflicting files, then continue:
pg-maker dev patch work continue
# Or restore the state before the pending operation:
pg-maker dev patch work abort
pg-maker dev patch work export --out patch-series
```

Each patch has a Git commit. Ordinary Git commits can be imported into the linear
series with `dev patch adopt`. Pop retains the unapplied tail; push reapplies it.
An external-component rebase uses `--scope component:ID --onto-source HASH`, referring
to a snapshot already in the session's InputStore. PostgreSQL rebase uses a new lock
and requires the same component repository set. Conflicted imports retain the rest
of the queue. Export writes format-patch files and `series.json`.

Before full release qualification, incorporate exported source changes and acceptance
tests into the regular release configuration and resolve again. Development snapshots
remain unverified.

## Retention

```bash
pg-maker dev pin work 000001
pg-maker dev prune work --keep-last 2 --out cleanup.json
pg-maker dev prune work --keep-last 2 --apply cleanup.json
pg-maker cache prune work/cache --out cache-cleanup.json
pg-maker cache prune work/cache --apply cache-cleanup.json
```

Prune retains working Git trees, current/pinned iterations, inputs, locks, journals
and outputs. It removes selected old build/checkpoint/debug scratch and releases
those runs' cache pins. A separate cache prune can reclaim unreferenced CAS files.
Global Docker images/volumes and other projects' inputs are untouched.

Core or bundled-contrib edits conservatively invalidate PostgreSQL's combined build.
External components are separate. Core headers/ABI, flags or image changes invalidate
affected extensions. Verified caches can supply successful results; partial object
files only accelerate a new make invocation. See [session contract](interfaces/development.md)
and [recovery](interfaces/recovery.md).
