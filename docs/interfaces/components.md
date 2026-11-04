# Default components

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
dependency. pg_wait_sampling can be selected independently. The engine does not add
components outside the feature list; `components: []` omits all three. pg_play runtime
profiles may select others explicitly. Resolve downloads and freezes external sources;
builds use locked inputs without networking.

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
