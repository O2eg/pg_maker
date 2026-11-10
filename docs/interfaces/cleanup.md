# Private scratch cleanup

`execution.cleanup.cleanup_plan(service, directory, exported) -> CleanupPlan`
requires terminal state, empty resource references and an external ExportManifest.
It verifies export inventory/hashes, state/input correspondence and current outputs.
The preview lists only workspace/build, workspace/source,
workspace/package-test-workspace and checkpoints, with inodes and allocated bytes.
Shared InputStore/CAS/Docker storage is outside this plan. Ordinary cleanup does
not release legacy shared-cache pins; a later cache prune may retain those entries.
Development-session pruning separately releases pins for its removed iterations.

`apply_cleanup(service, plan)` acquires the owner lock without waiting, repeats
export/state/path checks and rejects symlinks/mounts. Only listed scratch is removed.
Output, attempt logs, events and reports remain. `scratch-cleaned.json` prevents
future resume. The result records removed paths and free space before/after. A stale
plan cannot be reapplied; partial cleanup requires a new preview.

CLI: `cleanup OPERATION --export EXPORT --out PLAN`, then
`cleanup OPERATION --apply PLAN`. Ordinary-operation cleanup is not exposed through
MCP. Development-session retention has its own [CLI/API/MCP contract](development.md).
