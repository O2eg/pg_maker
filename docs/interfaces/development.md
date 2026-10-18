# Distribution development sessions

Development mode owns real Git repositories. A local repository can be copied with
history; commit/checkout/cherry-pick operate only in that copy. Archive/snapshot
inputs create a new Git repository with a base commit. Bundled contrib shares the
PostgreSQL tree; external components have separate repositories. Working sources
are separate from object directories, installed prefixes and CAS.

## Sessions and immutable iterations

`dev open DIRECTORY --lock LOCK` or `--from-run RUN` opens one feature. Optional
`--repository PATH` preserves local PostgreSQL Git history. Status reports paths,
HEAD, dirty state, series, conflicts and iterations. Session metadata operations
serialize through session.lock. Pending conflicts block builds.

Each `dev build` freezes source bytes into a new BuildLock/BuildPlan and iteration.
Records retain parent iteration/lock, commits, snapshots, operation and reuse reasons.
Uncommitted changes are allowed: locks reference exact bytes, never a mutable directory.
Later edits cannot alter a running iteration. Resume still retries the original plan
with original inputs. Corrected code creates a new iteration. Standard operation
status/events/cancel/resume apply to build runs.

## Patch stack

Scope `postgresql` covers core and bundled contrib; `component:ID` covers an external
extension, integrated source-tree component, library or utility. Patch actions are
import, commit, refresh, pop, push, adopt, rebase, continue, abort and export.
Successful patches have separate commits. Export preserves ordering and emits
format-patch plus a manifest containing base and identifiers.

Pop returns to an earlier commit while retaining the tail in stack.json. Push
restores it. To change a middle patch, pop back to it, edit and refresh; later commits
are replayed. Rebase moves a series onto an InputStore snapshot. PostgreSQL requires
a new BuildLock so version/catalog/environment remain consistent; the component
repository set must match. Component rebases select an existing source hash.

On conflict, the original stack, new base and remaining queue are retained. After
editing files, continue resumes the queue. Abort explicitly restores the prior state.
Saved refs retain old commits. Dirty work is not silently discarded by ordinary
stack movement. Manual linear Git commits require adopt; merge commits are rejected.
Git submodules must be resolved as source inputs before the working snapshot; gitlinks
are not supported as mutable development snapshot entries.

## Reuse and invalidation

Core identity includes PostgreSQL sources/patches, integrated source-tree components,
build image, flags, paths and implementation identity. External PGXS recipes do not
enter the core key. Component keys include the core key, own sources/compilation
recipe and declared dependency keys. Documentation rules do not enter compilation
keys. Core/header/ABI changes invalidate external components. An extension edit
invalidates that extension and its dependents, packaging and documentation.
Core/contrib edits currently invalidate the combined world-bin build conservatively.

Components run as separate steps. Verified successful outputs can cross lock
boundaries. Failed results never count as successful build evidence. Partial objects
may accelerate a new make invocation; changed source timestamps are refreshed and
incompatible environments/dependencies prevent object reuse. Cached component install
files cannot overwrite another component's or core's files. Component caches also
retain private-copy build workspaces for subsequent debugging and edits.

`dev build --component ID` includes selected components and transitive dependencies;
selection is restricted to build goals with tests off. Package/docs use the complete
feature. `dev explain` compares keys and explains changed inputs; unchanged inputs
mean a reuse opportunity, not a guaranteed cache hit. Opening a failed run can reuse
its verified core checkpoint and compatible component results.

`dev exec` runs argv in a separate owned operation, with a copy of the chosen stopped
iteration's workspace and its build environment. It never edits immutable inputs.
Source fixes belong in session Git trees.

## API, pg_play and storage

CLI and MakerService share DevelopmentSession. pg_play CLI/API/MCP delegate these
operations without a second scheduler. Long builds use the existing durable worker.
Development iterations remain unverified and cannot enable database qualification.

Python methods: dev_open, dev_status, dev_patch, dev_explain, dev_build, dev_execute,
dev_pin and dev_prune. Patch accepts action/scope and either one patch plus ID or a
series manifest. Dev execute creates operation_kind=development_command; it is not
resumable. pg_play names its corresponding method maker_dev_exec.

Current/pinned iterations are retained. Active operations and pending conflicts
block cleanup. Dev prune previews paths, fingerprints, size and plan hash; apply
revalidates under session and affected owner locks. It removes only selected old
scratch/debug directories, preserving sources, Git history, inputs, locks, logs and
reports. Free-space reserves are checked before snapshots/copies. No global Docker
prune or unrelated-directory cleanup occurs. Shared InputStore collection remains
an explicit separate responsibility.
