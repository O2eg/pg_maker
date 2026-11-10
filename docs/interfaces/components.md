# Default components

External components can declare ordered `source_patches`. Each entry is either a
relative patch filename or a mapping with `path` and `compatible_postgresql`
(a Python packaging version specifier, for example `'>=18.6,<19'`). Configuration
loading selects matching patches for the requested PostgreSQL version; only those
files become hashed inputs in BuildLock and participate in source resolution.
Unmatched entries require no patch file. Invalid version specifiers fail validation.
Use this for maintenance-specific test configuration while retaining the same
extension source version and mandatory test coverage.

Init creates editable `components/*.yaml` and `documentation/*` alongside the
`my-feature` configuration. The feature explicitly lists its components:

| Component | Source | Version |
| --- | --- | --- |
| pg_stat_statements | Selected PostgreSQL contrib | Matches server sources |
| pg_stat_kcache | powa-team/pg_stat_kcache | 2.3.2; commit 1e39814453ab747a3817cfdf4f6f6bd5c0fb9507 |
| pg_wait_sampling | postgrespro/pg_wait_sampling | 1.1.11; commit 7e3a5fc937393596c0a51c5881dd63d7208408d1 |

The SQL extension version of pg_wait_sampling remains 1.1. Recipes declare PostgreSQL
14–19, including beta/RC. Declared compatibility is distinct from executed validation;
see [default-component evidence](../validation/default-components.md). PGXS uses the
current build's private headers and pg_config. Libraries, control/SQL and licenses
are assigned to the server DEB.

pg_stat_kcache requires selected pg_stat_statements; validation rejects a missing
dependency. pg_wait_sampling can be selected independently. `components: []` removes
the three explicit component recipes, including the two external extensions. With
`packaging.standard_contrib: true` (the default), upstream contrib files such as
pg_stat_statements still ship in the server package. Select `standard_contrib: false`
as well when deliberately creating a minimal custom distribution. pg_play runtime
profiles may select others explicitly. Resolve downloads and freezes external sources;
builds use locked inputs without networking.

Optional `test_dependencies` and `documentation_dependencies` supplement
`build_dependencies` only when their stages are enabled. Optional
`extra_source_roles: {clickhouse: test}` classifies named extra sources as `build`
(default), `test` or `documentation`; disabled-purpose sources are excluded before
downloads and locking. Dependencies used to generate installed SQL or required by
configure remain build dependencies even when documentation/tests are disabled.

Installers must honor `DESTDIR`: Make receives an explicit assignment, CMake and
custom `build_commands` receive the environment variable. A custom installer must
place its payload at `$DESTDIR` followed by the absolute runtime prefix. Empty
staging fails instead of silently accepting an installer that ignored DESTDIR.
Staging mirrors prefix directories only, for PGXS hooks that expect existing header
directories. Files are published only after the install command succeeds. A durable
intent supports replay after interruption, ownership records prevent conflicting
overwrites, and files removed by a new component version are retired. Tests execute
against the published prefix and rerun on every testing iteration.

Component documentation rules add a DocBook page to the contrib appendix. It appears
only for selected components with documentation enabled and participates in shared
navigation, search, HTML/PDF, link checks and translation. Sources and documentation
assets enter BuildLock; recipe/manual changes require resolution. Guides are maintained
with the pinned extension version; README import is not automatic.

Installing files does not change existing PGDATA or start collectors. Administrators
select preload libraries, restart the server and create SQL interfaces in each needed
database. When used together, preload order is
`pg_stat_statements,pg_stat_kcache,pg_wait_sampling`. Recipe preload values drive
component checks; they do not edit production configuration.

Database suites and native installcheck run by default. An explicit skip produces
unverified results. Non-PGXS recipes use explicit test_commands: each command needs
an id, argv and success_pattern; a named `count` regex group must be positive.
Commands may set cwd/env. Optional test_setup runs prerequisite commands; test_cluster
selects a temporary cluster (default true), with preload, test_settings and test_database.
Pre-core library tests require test_cluster=false. Test helpers are available through
`{test_helpers}`; `{port}` and `{user}` refer to the temporary cluster. Make-based recipes
can set test_make_args for upstream test-only overrides. Installed checks verify every
sql_extension_versions entry in isolated databases; installed_database handles extensions
such as pg_cron that require a specific database. Existing projects are not silently changed: copy
components/documentation from a fresh init, add feature references, then prepare
environments and resolve. Reinitializing modified files reports a conflict.

For inventoried Make-based PGXS/contrib/source-tree tests, `allowed_tap_skips`
can declare an operator-approved upstream skip-all. Each entry requires an exact
relative `t/*.pl` path, `source_sha256`, exact `skip_reason`, explanatory `reason`
and `approval`. Resolve locks the declaration with the recipe. Before execution,
the engine verifies the file hash and inventory membership. The exception only
accepts a clean zero-test TAP skip with that exact reason; missing results,
failures, parser errors, nonzero exit/wait or changed test files still fail.
It never suppresses test execution. A test that actually runs must pass normally.

Each Make target writes `test-evidence.json` with executed regression/isolation
counts, executed TAP assertions and `excluded_tap_suites`. The component report
also exposes `test_exceptions`; excluded entries have `counted_as_passed=false`.
A passing component with declared exceptions means all remaining tests passed,
not that excluded scenarios executed. At least one real test must pass. Custom
`test_commands` cannot use this policy because their inventories use separate
adapters. An ordinary build with all database tests disabled remains unverified.
