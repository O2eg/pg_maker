# Source changes

`changes.loader.load_changes(references, base, version)` and `load_components(...)`
validate documents, compatibility, requires/conflicts, unique IDs/revisions and
explicit files. Stable topological order preserves available independent-node order.
Operations and acceptance scripts have logical input names and hashes; resolve copies
them into InputStore. Required tests merge without allowing different acceptance
checks to share an ID.

`changes.apply.apply_changes(source, feature, lock, store, branch, receipts, check)`
applies changes; check observes cancellation. Each group works on a separate tree
copy and publishes only after every operation and postcondition succeeds. Receipts
record input/output hashes, diff, changed paths and applied/already_satisfied status.
Already-satisfied is limited to the built-in socket patch's exact result.

Resolve creates the common patch against current sources. The target full-line
socket definition uses `/var/run/postgresql` and a comment describing shared server/
libpq use. Existing configured lines are normalized to this representation; repeating
the exact result does not change the tree.

Groups and operations preserve explicit ordering. UTF-8 unified patches apply without
shell, offsets or fuzz. Supported inputs include diff -u, git diff and git format-patch,
with regular-file additions/deletions (100644/100755). Existing-file mode changes,
binary changes, rename/copy and symlink patches are unsupported in this strict
release ChangeSet adapter. The development Git stack has a separate Git-based adapter.

Text insertion verifies cardinality, top/bottom order, context and optional input
hash. Each top matches the first bottom before the next top; unrelated later table
markers do not introduce ambiguity. Repeated insertion without a receipt fails.
Changes cannot escape the source tree or traverse symlinks. Successful publication
removes temporary source.before-N copies. Failure/cancellation preserves original
inputs and never reapplies a partially modified group.

New actions require explicit registry entries, versioned schemas and positive/negative
fixtures. The only built-in source patch unifies the Unix socket directory; branding
is explicit data.

## Agent workspaces

`changes.workspace.workspace(input_store, source_hash, destination)` copies a
verified snapshot into a new destination/source and writes workspace.json. It checks
source size plus a 4 GiB reserve and never hardlinks CAS. `export_changes(directory,
output, id, postgresql)` compares against the verified base and emits ChangeSet plus
unified diff. UTF-8 additions/deletions/edits, including no-final-newline files, are
supported. Binary/symlink/mode changes are rejected. New file permissions follow umask
when applying; empty directories do not appear in diffs.

`validate_changes(file)` validates schema/referenced operation files and returns
hashes with acceptance_executed=false. Export does not assert ABI/data/WAL compatibility;
all remain unknown until separately specified and tested.

With `workspace(..., lock=BuildLock, feature=ID)`, the base includes common and existing
feature changes, stored as a separate CAS tree. `until="CHANGE_ID/OPERATION_ID"` stops
before an operation for adapting it. Insert the resulting diff at that position;
do not blindly append it. Without a lock, basis remains raw upstream. Export verifies
the frozen base independently of later lock-file edits. Validation also checks
insertion schemas and acceptance script hashes/existence; execution is separate.
CLI workspace/export/validate and pg_play MCP share these fields.
