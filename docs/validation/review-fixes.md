# Review findings checked — 2026-10-17

The workspace review was compared with implementation and specification. All four
P0 findings were reproduced and fixed. This report supplements [integration evidence](integration.md).
It records the review's historical status; later capabilities appear in the roadmap.

| Finding | Result |
| --- | --- |
| P0-1: GUC insertions | Match first bottom after top and before next top. Golden compact/expanded fixtures cover repeated bottoms; examples are original parser fixtures, not release-specific database excerpts. |
| P0-2: Git patches | Added/deleted regular/executable/empty files and format-patch supported. Rename/copy, binary, links and existing-file mode changes explicitly rejected. |
| P0-3: pg_play recipes | Inventory includes four SQL extensions' control/SQL and auto_explain library. Fixed pg_stat_statements targets/preload declarations. Runtime requires installation SQL, not only control/library. |
| P0-4: package goal | Runtime repository exported without docs/database tests; exact set/hashes checked, links/extra files rejected. |
| P1-1: dependencies | Actual shlibdeps plus service dependencies; undeclared ELF dependency fails, unused declaration warns. libpq SONAME checked; unconditional systemd dependency removed. |
| P1-2/3: configure | Added optional compression/XML/PAM/systemd/UUID/language/LDAP/GSSAPI dependencies. Effective configuration checked; unknown options rejected. Warnings reported as inventory. |
| P1-4: sockets | Canonical /run tmpfiles, missing-directory creation, mode 2775; harness checks server/libpq defaults without host overrides and recreation. |
| P1-6: workspace | Lock/feature define already-patched base; until stops before a chosen operation. CAS pins the base; validation covers insertion/acceptance files. |
| P1-7: acceptance | Real verbose-psql SQLSTATE, exit 3 only with explicit SQL-error assertion, optional baseline expectation and consistent prefix expansion. Connection failure is not an expected SQL error. |
| P1-8/11: harness/plan | Baseline check-world removed, baseline-core evidence retained. Crash sleep replaced by bounded PID/socket polling. Limited custom systemctl harness documented. |
| P1-9: pg_play | Typed preparation/resolution overrides, changes commands and lock/feature/until; runtime files/entrypoint/extensions supplied by profile. |
| P2 | Default goal build, supplied-plan goal inheritance, source.before cleanup, Git archive attributes neutralized, service enable only for initialized PGDATA. |

Installation additionally exposed Debian slim's documentation exclusion: licenses
existed in DEBs but disappeared after install. Runtime and installation validation now
include product documentation paths explicitly; repeated installation retained licenses.

## Executed validation

219 maker unit/contract tests and 125 pg_play tests passed. The initially skipped
pg_play Docker test was then run with a real worker. Two maker Docker tests compiled
a small ELF fixture and exercised DEB/manifest/export/cache plus resume after SIGKILL.
Fixture database targets deliberately fail and were not invoked. Runtime-repository
inventory was checked before/after export and repeated separately. Git-generated
format patches and commits with export-ignore/export-subst were checked. Actual 18.1
configure output was verified; negative options had unit coverage. Both projects
passed Ruff, distribution build and Twine.

Backend packaging reused compiled PostgreSQL 18.1 core/contrib, rebuilt external
wait-sampling/kcache from pinned sources using private PGXS, then ran current
InstallTree/dependency/DEB code with pg_play recipes, package goal and docs disabled.
Files were not manually inserted into finished DEBs. This was a branch-fixture backend
check, not a new prepare/resolve/compile production run; those packages were not releases.

Fresh offline containers installed libpq/client/server sets with run_database=false.
Inventory, loader, dependencies, no implicit initdb, empty-PGDATA startup rejection and
removal passed. No full cluster started. Public PgPlayService built the runtime offline;
version, private pg_config, libraries/licenses and absent PG_VERSION verified.
Qualification stayed unverified.

## Limits and remaining work at review time

Database startup, CREATE EXTENSION, check/installcheck, JIT/NLS/locale, dump/restore,
crash/upgrade and coinstallation were not exercised live. Harness fixes did not qualify
the entire database. Configure options were available explicitly; the full matrix was
not compiled. LDAP/GSSAPI tests still needed fixtures. Further requirements included
PG_MAKER_* context, requirement/evidence mapping, richer assertions/setup, manifest
entrypoints, additional MCP operations and experiment baseline/candidate support.
The standalone maker MCP server remained unimplemented; current MCP is in pg_play.

Other deferred concerns included service refactoring, cache/hash optimization,
coinstall/CPU limits, extended test profiles and log rotation. The test image contained
systemd but used the emulator; native lifecycle remained unqualified. Base tags resolve
during preparation; execution uses immutable IDs. Pinning the original registry digest
was a separate reproducibility improvement. P2-4 was not reproduced: malformed CLI JSON
returned validation_error/exit 2. Earlier reports already disclosed fixture DEBs and
missing full database acceptance.

Compact scripts/logs/runtime export were retained in /tmp/pg-maker-review-delivery-20261017.
Test-owned trees/CAS/images were removed afterward; earlier images were retained.
