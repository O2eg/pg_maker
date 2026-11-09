# PostgreSQL 16.15 with private ICU 70.1

Physical binary compatibility with the managed source is **not verified**.
The [clone compatibility contract](yc-clone-compatibility.md) applies to this profile
and to previously built packages; build/package qualification is a separate result.

All maintenance profiles include the complete standard PostgreSQL contrib, including
optional UUID, SELinux and Perl/Python transform modules. Contrib is inventoried
independently of the managed extension catalog and delivered in the server DEB;
`auto_explain.so` does not need a CREATE EXTENSION command. PLV8 stays disabled.


This profile targets Debian 13 / x86_64 / DEB. It builds upstream PostgreSQL 16.15
with source-pinned ICU 70.1, installs the required ICU runtime libraries inside the
product prefix and selects public-source extensions from the managed catalog.
It does not reproduce Yandex patches or claim vendor binary compatibility.

`yc-16.14-icu70` selects PostgreSQL 16.14 with its official archive SHA256 and
otherwise uses the same settings and component recipes as `yc-16.15-icu70`.
The extension catalog remains the audit of the 16.15 source system. Examples and
historical qualification records below refer to 16.15.

## Installation paths

Newly initialized profiles use `/opt/postgresql-yc-clone/16/bin`,
`/var/lib/postgresql-yc-clone/16/data` and
`postgresql-yc-clone-16.service`. Debug builds append `-debug` to the major.
The feature ID `yc16-icu70` remains an internal recipe selector. Configure the
paths in the generated `config/defaults.yaml` and vendor in `config/vendor.yaml`.
Existing projects and previously built DEBs retain their original settings; they
require a new resolve/build. Earlier qualification records do not qualify the new
layout. See the [build, upload and install walkthrough](yc-clone-packages.md).

## Create and build

```bash
pg-maker init yc16 --profile yc-16.15-icu70
pg-maker validate yc16/build.yaml
pg-maker config show yc16/build.yaml --resolved
pg-maker env prepare yc16/build.yaml --input-store yc16/inputs --out yc16/environments.json --operations yc16/operations
pg-maker resolve yc16/build.yaml --environments yc16/environments.json --out yc16/lock.json --input-store yc16/inputs --operations yc16/operations
pg-maker plan yc16/lock.json --goal package --out yc16/plan.json
pg-maker build yc16/lock.json --plan yc16/plan.json --operations yc16/operations --detach
```

Run as the non-root UID/GID in `config/defaults.yaml` (default `1000:1000`). All
project files remain editable. The standard `init` still creates the 18.1 profile.
Reserve substantially more than the configured 20 GiB minimum for the complete
extension set, especially PostGIS/GDAL, inputs and checkpoints. Enabling the optional
PLV8/V8 recipe adds substantial build time and disk usage.
The earlier package set passed native amd64 source, extension and installed-package
qualification on 2026-11-05, including the full test profile. See the
[qualification record](managed-16-icu70-qualification-20261105.md) for exact package
hashes, test coverage, warning review and limitations. PLV8 is disabled in this
build. The historical 2026-11-03 acceptance below covers earlier packages with PLV8.
The catalog retains `selected-unverified` for newly initialized projects:
qualification belongs to a specific operation and its immutable artifacts, not
to future builds merely selecting the same recipes.

## Version policy

The profile contains:

```yaml
postgresql:
  version: '16.15'
product:
  release: 0
  package_revision: 1
  version_format: postgresql
```

Product version is `16.15`; the Debian package version remains `16.15-1.deb13`.
The latter suffix is packaging metadata, not an extra PostgreSQL version component.
Set `version_format: three-part` to get `16.15.0`; this remains the backward-compatible
default when omitted. PostgreSQL format requires `release: 0`, avoiding silently
discarded vendor release numbers. Increment package_revision for rebuilds.
PostgreSQL's own `version()` and pg_config version are taken from upstream sources.

## Package names

Since 2026-11-05, the profile declares per-role names in `spec.naming.package_by_role`
in `build.yaml`, following Debian/PGDG names with the suffix `yc-clone`:

| Role | Package built on 2026-11-03 | Current profile name |
| --- | --- | --- |
| Server | `test-yc16-icu70-server-16` | `postgresql-16-yc-clone` |
| Client | `test-yc16-icu70-client-16` | `postgresql-client-16-yc-clone` |
| libpq | `test-yc16-icu70-libpq5-16` | `libpq5-yc-clone` |

For example, the server filename becomes
`postgresql-16-yc-clone_16.15-1.deb13_amd64.deb`. The optional documentation package
is `postgresql-doc-16-yc-clone`. Debug builds insert `-debug` before `-yc-clone`.
The `5` in libpq5 is its ABI version; it is not a PostgreSQL major. This package
name allows one libpq clone version per architecture rather than parallel packages
for each PostgreSQL major.

This naming is a local packaging convention, not an official PGDG release or proof
of managed-server compatibility. Package versions retain the local `deb13` suffix;
the profile does not claim the PGDG `pgdg13` package origin. Server/client internal
dependencies use the new names. Installation paths and service names retain their
separate configuration. Existing DEBs, archived locks and acceptance evidence keep
their original names; the new names require a fresh resolve/plan/build.

The profile enables `spec.packaging.server_dev_files: true`. Headers (including
server and internal headers), PGXS, static support libraries, pkg-config files and
the libpq linker symlink ship in the server package. No separate devel package is
created. Use this installation's `bin/pg_config` when building external extensions;
install the compiler and the extension's own dependencies separately. Existing
packages must be rebuilt to obtain this payload.

See [PGDG Debian packages](https://www.postgresql.org/download/linux/debian/) and
[the libpq packaging policy](https://wiki.postgresql.org/wiki/Apt/FAQ).

## Pinned library mechanism

`components/libraries/icu70.yaml` pins the upstream ICU 70.1 source archive by SHA256.
It also backports upstream
[ICU-22779 / 45a8a28](https://github.com/unicode-org/icu/commit/45a8a28de6fa28af8e03c9c88473500dc51ca498),
which initializes the collation builder's unused trie pointer without reading an
uninitialized member. The patch changes two C++ files, not ICU's Unicode/collation
data or its version. GCC 14 diagnosed the original initialization-order defect.
The recipe is part of the selected feature and is resolved into the immutable lock.

```yaml
type: library
build_phase: before-postgresql
pkg_config_versions:
  icu-i18n: '70.1'
  icu-uc: '70.1'
provides_options: [icu]
```

Execution builds/installs the library before PostgreSQL configure, checks exact
pkg-config versions and private libdir, and records `library_versions` in the report.
The core and every dependent component use the private library search paths.
Changing library sources, version assertions or build options invalidates core reuse.
System ICU is not downgraded: the feature removes the direct libicu-dev/libicu76
requirements; other platform libraries may still need system ICU independently.

The server DEB includes libicudata.so.70, libicuuc.so.70 and libicui18n.so.70 with
their versioned targets. With server_dev_files enabled, development headers and
linker symlinks for the selected libraries are also included; unselected ICU tools
remain excluded.
RPATH is checked by the existing ELF/package validator. Installing a package never
initializes or starts a database automatically.

The mechanism accepts other source-pinned Make/Autotools libraries with private
pkg-config files. A provider must correspond to an enabled PostgreSQL option;
multiple providers for the same option or dependencies on post-core components fail.
It does not infer unspecified dependency versions or enable network access in builds.

## Managed extension coverage

The source inventory is the 2026-11-02 non-contrib catalog snapshot from
a managed PostgreSQL 16 reference database.
Only pg_repack was installed in that database; the list describes availability.
The editable [catalog](https://github.com/O2eg/pg_maker/blob/main/src/pg_maker/bundled/catalogs/yc-16.15-extensions.yaml)
retains all 38 names, default/installed/catalog versions, source pins, selection and
exclusion reasons. The profile selects 33 SQL extensions through 25 source recipes,
plus contrib pg_stat_statements, contrib fuzzystrmatch (required by the Tiger geocoder)
and ICU. No missing component is silently substituted.

SQL extension versions are not necessarily project release versions. For example,
pg_cron 1.5.2 plus upstream compiler fix `0cb8118` supplies SQL version 1.5;
RUM 1.3.14 supplies 1.3; jsquery commit `40b1db5` includes its PostgreSQL 16
compatibility fix and retains SQL version 1.1. AGE uses the PG16/v1.5.0-rc0
source tag whose control file declares 1.5.0.
Repositories without a release tag are pinned to a specific commit. These choices
match control versions, not a proven managed implementation/patchset.

PostGIS supplies seven catalog entries. pglogical also supplies pglogical_origin;
h3 supplies h3_postgis. CMake recipes use explicit private pg_config paths. H3's
core source is a separate hash-pinned input passed via FETCHCONTENT_SOURCE_DIR_H3,
with FetchContent downloads disabled. Git submodules of selected components are
frozen by the existing resolver before compilation.

PLV8 3.2.3 is disabled by commenting out its component in
`config/features/yc16-icu70.yaml`; its catalog status is `disabled`. Its recipe and
sources remain described in the inventory. To opt in, uncomment the component,
change its catalog status to `selected-unverified`, remove the disabled reason,
and prepare/resolve a new lock and plan. Existing locks still select their original
components and must not be reused to request a build without PLV8. Already initialized
projects keep their copied configuration and do not automatically inherit profile edits.
The retained PLV8 recipe uses the bundled
[source patch](https://github.com/O2eg/pg_maker/blob/main/src/pg_maker/bundled/patches/plv8-3.2.3-gcc14-and-resume.patch).
It adds the explicit `<algorithm>` include required by GCC 14 and always completes
the nested incremental V8 build, using the parent make jobserver. SQL version 3.2.3
is retained. The resolver records patch hashes, the original source tree and the
patched tree; it does not modify cached upstream inputs. This fix is newer than
the published pg-maker 0.2.0 release.

The paused emulated build used the same patched tree as a frozen local snapshot
to retain already compiled objects with its original controller. Its provenance
records the upstream commit, patch hash and patched tree hash. A fresh build from
this checkout applies the patch through the recipe. For long QEMU builds, set
`spec.execution.limits.stage_timeout_seconds: 21600` in `config/defaults.yaml`.

The pgRouting 3.5.0 recipe also applies
[a GCC 14 compatibility patch](https://github.com/O2eg/pg_maker/blob/main/src/pg_maker/bundled/patches/pgrouting-3.5.0-gcc14.patch).
`SPI_getvalue` already returns `char *`; the patch removes the incorrect
`DatumGetCString` wrapper rejected by GCC 14. The SQL extension version remains
3.5.0, and the patch and resulting source tree are recorded in the lock.

The logerrors 2.1 recipe includes its upstream `2.0--2.1` SQL upgrade script in
the Makefile install list. It also explicitly exports the `logerrors_main` worker
entry point: PGXS uses hidden symbol visibility, and without the export the
library loads but the background worker fails to start. The functional test
checks interval error aggregation as well as the three zero-valued TOTAL rows
retained after statistics reset. Both source patches are recorded in the lock.
PostGIS packaging requires `postgis_raster-3.so` and
selects contrib fuzzystrmatch so `postgis_tiger_geocoder` can be created with CASCADE.

Four entries require additional inputs:

| Extension | Managed SQL version | Status and action |
| --- | --- | --- |
| anon | 1.3.2.1 | Exact vendor variant unconfirmed. Supply matching sources/recipe; do not relabel 1.3.2. |
| lwaldump | 1.0 | Matching public source not located. Supply vendor sources. |
| oracle_fdw | 1.2 | Pinned upstream recipe is provided but not selected. Provision Oracle Instant Client SDK/runtime, paths and runtime dependency policy before enabling it. |
| pgvector | 0.8.2 | Managed alias has no separate upstream control/library in the vector release. Upstream vector 0.8.2 is selected. Do not fabricate an alias for physical catalog compatibility. |

Additional patches/libraries needed by managed preloads such as repl_mon are outside
this non-contrib SQL catalog; this profile is not a drop-in managed-server clone.

## Acceptance before using physical data

Source version pinning and control-file checks do not qualify physical replication.
On an isolated fresh database initialized with ICU en-US, require:

```sql
SELECT datlocprovider, daticulocale, datcollversion AS stored,
       pg_database_collation_actual_version(oid) AS actual
FROM pg_database WHERE datname=current_database();
-- Expected: i | en-US | 153.112 | 153.112
SELECT 'O''Connor' < 'O’Brien';
-- Expected: true
```

Check loaded libicu paths of the running postgres process, all used explicit
collations, extension compatibility and index-versus-sequential query results on an
isolated physical pair. Matching collation version alone does not establish identical
ICU data or vendor patches. Do not use REFRESH COLLATION VERSION to hide a mismatch.
Default LC_COLLATE/LC_CTYPE and any libc-based objects need separate compatibility
checks when the OS differs. Physical PGDATA must never be initialized with initdb.

The current controller enables tests by default and adds PostgreSQL 16 regression
parsing, ICU make check, PGXS/TAP and explicit CMake/native component suites. The
package goal also installs the exact DEBs in fresh offline containers. Use
`pg-maker test-full yc16/lock.json --operations yc16/operations` to add expensive
WAL consistency checks to the standard source suites. The
[2026-11-05 qualification](managed-16-icu70-qualification-20261105.md) passed with
standard and full suites enabled in the package build. The historical manual
acceptance below covers the earlier 34-extension package only. Every new build
still requires its own successful source, extension and installed-package report.

Installed-package checks preserve the PostgreSQL startup and collector logs before
removing their containers, including failed startups, and apply the configured
generated-log diagnostic policy to them. The pg_hint_plan recipe preloads only
pg_hint_plan and pg_stat_statements. Its upstream FDW test creates file_fdw when
needed; file_fdw is not a required preload or a delivered component of this profile.

When enabling many preloaded extensions together, size `max_worker_processes` for
their workers and parallel queries. The all-preload package test recorded a
TimescaleDB job-registration warning at the upstream default of 8. A supplementary
fresh-container check of the same DEBs with `max_worker_processes=32` executed a
scheduled TimescaleDB job twice, with zero failures. This is a tested configuration
example, not an automatic change to the PostgreSQL default or a universal capacity
recommendation.

pg_partman tests run with pinned pgTAP, including staged procedures/reindexing,
non-superuser, tablespace and background-worker scenarios. Upstream's two
not_working_yet scripts and its PostgreSQL <=11 tablespace variant are explicitly
not applicable; the pg12+ tablespace scenario runs. Its native-reindex scripts are
also not applicable: `reapply_indexes.py` explicitly rejects native partition sets
on PG11+. The legacy reindex suite runs in full. These cases are recorded separately
from successful assertions. The logerrors pin has no upstream
regression target, so a functional helper checks statistics, error capture and reset.
These recipes are test definitions, not evidence of a passed run. Preparing a new
environment is required for CUnit, pgTAP and Python test dependencies.

The pg_partman helper uses a pinned, locally compiled libfaketime solely for its
test cluster and clients. The calendar-sensitive fixtures run at a mid-month Friday;
the shared day offset targets 2026-05-15 while time continues advancing and
monotonic clocks remain real. This avoids the documented weekday/year-boundary
fixture failures. A separate test-only patch fixes half-minute expectations in
the two 30-second scenarios: they must use `floor(seconds / 30)`, not
`floor(minutes / 30)`. Table, row-count and privilege assertions remain enabled. The native daily test
also adds its foreign key to the partitioned parent on PG11+, after its initial
no-FK assertions; future-child FK checks therefore exercise native inheritance. The host clock and
delivered PostgreSQL libraries are unaffected. The test cluster also uses
`max_locks_per_transaction=1024` for the three-level subpartition scenarios.

Other test-environment adaptations are explicit: ICU's Python test loader supports
Python 3.13; pg_repack gets the four upstream tablespace fixtures; spqrguard runs
its five regression cases and isolation case without attempting its nonexistent
TAP files. The pglogical recipe ports its TAP harness to PostgreSQL 16 APIs and
allows `pglogical_output` in its disposable test clusters' `output_plugin_libraries`
setting (PostgreSQL 16.15+). Legacy TAP clusters explicitly configure their
socket directory and subscriber port after the modern test module resets inherited
connection variables. The subscriber TAP test explicitly uses the installed tool
next to `pg_basebackup`, independent of PGXS's source-first PATH. Its basic SQL
uses PG16 WAL function/column names, a 180-second statement timeout and
`ON_ERROR_STOP` so SQL failures cannot pass unnoticed. `regresscheck` avoids the unrelated unsupported PGXS `check`
target. It also backports upstream fix
[`7acb024`](https://github.com/2ndQuadrant/pglogical/commit/7acb02405be5471d9827bd62b37c810038e262e3)
for a table-sync worker hanging on an idle source. The upstream SQL version remains
2.4.4; source provenance records the patch. Its regression synchronization budget
is 180 seconds, as in current upstream. These changes do not count failed tests as successful.
The pglogical TAP time-travel scenario retains its upstream TODO/SKIP marker.

The recipe also fixes a reproduced resynchronization race in pglogical: an apply
worker waiting behind the resync's TRUNCATE lock could use a stale table-sync cache
and insert a row that COPY would insert again. The local runtime patch refreshes
pending sync state after acquiring the target relation lock. Its deterministic
TAP scenario queues a provider insert behind that lock: the unpatched version
failed all three row-count/data comparisons; the patched version passed all nine
assertions. This is a local source modification, not a claim of vendor-binary identity.
The load test explicitly names its pgbench connections so resync continues throughout
the load, and compares full table contents after catch-up in addition to row counts.

PostGIS 3.4.0's tests include upstream normalization fixes
[`777b0f7`](https://github.com/postgis/postgis/commit/777b0f7e91f0d83254e5de77d73e73df41dfb706)
and [`cc695fd`](https://github.com/postgis/postgis/commit/cc695fd305a9e3ec2003026d8e63d92887ab2009).
On GEOS 3.13+, the RelateNG reference from
[`d4e89be`](https://github.com/postgis/postgis/commit/d4e89be9cf1b857885d28e315c9d117e62f179f5)
is selected for boundary-node tests; its first-column values also cover the four
identical invalid-MultiPolygon inputs in `relate.sql`. Original references remain
available for older GEOS. All SQL scenarios remain enabled, and failed diffs are
preserved in the component workspace. These changes affect tests only, not installed
PostGIS functions or its SQL version. The build environment includes `xmllint` and
DocBook XML so the upstream documentation checks also run.

pgRouting's pgTAP database is created before invoking its upstream fixture script.
Its test-only patch follows upstream
[`074647e7`](https://github.com/pgRouting/pgrouting/commit/074647e7e9fc8a8b8082e8049ba9710a05c474d7)
and [`97c1ae53`](https://github.com/pgRouting/pgrouting/commit/97c1ae530353943795934ff61b8b7c75eccc850e)
for the mincut selected by Boost >=1.80. The withPointsDD fixture permits either of
two equal-cost incoming edges to node 12 and separately verifies both edges and
their predecessor distances; node, edge-cost and total-distance assertions remain.
No routing algorithm changes or failed-case exclusions are involved.

TimescaleDB's test targets run serially because they share `testcluster/data`;
compilation still uses the configured job count and regression schedules retain
internal parallelism. Test sessions use upstream's `US/Pacific` timezone and UTF-8
encoding, including non-ASCII collation tests. A test-only PG16.15 reference patch
expects the ACL error to name a `view` instead of a `materialized view`; the denied
access assertion is retained. This changes neither the database package's timezone
nor the installed TimescaleDB code.

TimescaleDB source archives also receive the pinned upstream commit hash and
commit timestamp through explicit CMake metadata. The tag has a `-pg-maker`
suffix to identify the patched source. This preserves the build-info tests
without fabricating a Git repository or weakening their assertions.

Its PG16 fixtures backport upstream fixes
[`dedb1c7`](https://github.com/timescale/timescaledb/commit/dedb1c70ac88e037b9a74aa77c33c02ea3e07933)
for COPY end markers after errors and
[`8064415`](https://github.com/timescale/timescaledb/commit/8064415d552a481cbc7077eb9f462a88b43fcd51)
for deterministic ordering in the ChunkAppend projection case. That case locally
disables index scans to preserve its sequential-scan reference plan; subsequent
index tests retain their original settings. All RLS and optimized/unoptimized
result comparisons remain enabled.

The public pg_stat_query_plans pin needs explicit PG16 runtime fixes: the plan
reader now uses the same `pg_read_all_stats` privilege check as the SQL reader;
PG16 uses the pin's newer utility hook, which preserves nesting depth even when
utility tracking is off and saves statement metadata before transaction cleanup.
Analyzed cursor, CTAS and EXPLAIN child queries receive IDs during parse analysis
on PG16. Materialized-view rewrites remain untracked, and the planner does not
override deliberately cleared IDs. These are local modifications; SQL version
1.0 alone does not identify the patched implementation.

All twelve upstream regression scenarios run. PG16's EXPLAIN row-count formatting
and retained leading comments have explicit references. The privilege reference
requires ordinary users to see only their own rows, matching the implementation's
restriction, and adds an explicit negative check for both statistics views.
Because PG16 lacks `MERGE RETURNING`, that fixture executes MERGE and COPY
separately while checking both data and tracking levels. A test-only libpq client
performs the same seven named/unnamed protocol executions and validates every
returned value; the original statistics counters are still checked. This replaces
newer psql's `\parse`/`\bind_named` commands without dropping protocol coverage.
The helper is not installed. Upstream `TAP_TESTS=0` is overridden to empty because
make treats `0` as enabled although this pin has no TAP files.

### Compiler version and physical replication

| Build | Compiler |
| --- | --- |
| Managed `16.15-201-yandex.58146.d4afcfb9ca` (reported server version) | GCC 11.4.0, Ubuntu `11.4.0-1ubuntu1~22.04.3` |
| Native package built on 2026-11-03 (version string extracted from packaged postgres) | GCC 14.2.0, Debian `14.2.0-19` |

Matching the compiler version is not itself a physical-replication requirement.
The relevant constraints are compatible architecture/data layout, PostgreSQL data,
catalog and WAL formats, and compatible runtime behavior. GCC documents ABI and
alignment as compatibility properties; compiler flags can change them. Thus a normal
x86_64 GCC 11-to-14 change alone is not grounds to expect broken WAL replay, but
matching GCC 11 does not establish compatibility with Yandex's patched server either.
This is a compatibility assessment, not evidence from a managed-to-upstream replica test.

Compare data-layout parameters such as block sizes, relation segment size and
alignment, and check any vendor changes to catalogs, WAL and extension storage.
Build extension shared libraries for the receiver's PostgreSQL and runtime libraries.
ICU and libc collation behavior must match for every collation actually used;
the matched ICU 70.1 en-US version `153.112` does not validate libc collations.
Qualification still requires an isolated physical replica with WAL replay and
application/index checks against the managed source.

Omitting PLV8 does not by itself change PostgreSQL's core WAL format. It does mean
PLV8 functions cannot run on the receiver; a configuration that preloads the missing
library must also be corrected before startup. Inventory installed extensions,
functions and preloads across all source databases before using this profile as a replica.

References: [PostgreSQL standby planning](https://www.postgresql.org/docs/16/warm-standby.html#WARM-STANDBY-PLANNING),
[GCC binary compatibility](https://gcc.gnu.org/onlinedocs/gcc/Compatibility.html),
[PostgreSQL control-file format](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/include/catalog/pg_control.h),
[collation version changes](https://www.postgresql.org/docs/16/sql-altercollation.html).

Source references: [PostgreSQL 16.15](https://ftp.postgresql.org/pub/source/v16.15/),
[ICU 70.1](https://github.com/unicode-org/icu/releases/tag/release-70-1),
[PostGIS 3.4.0](https://download.osgeo.org/postgis/source/postgis-3.4.0.tar.gz),
[h3-pg 4.1.3](https://github.com/postgis/h3-pg/tree/v4.1.3).
Other per-extension source URLs and immutable refs are in the catalog.

## Validation recorded on 2026-11-03

- 288 unit/contract checks and one small native CMake integration check passed. These cover
  both version formats, rejection of discarded release numbers, the complete
  catalog, pre-configure ordering, cache invalidation, frozen auxiliary inputs,
  private-library/version failures and extension control-version failures.
- The real ICU 70.1 archive was compiled/installed by the component builder in an
  isolated local directory. A linked ICU C probe using ucol_open("en-US") and
  ucol_getVersion returned **153.112** on the aarch64 workstation. This verifies
  the source/library path, not the target amd64 PostgreSQL executable or DEB.
- After disk cleanup, Debian 13 amd64 compilation was started under QEMU on the
  aarch64 workstation, with four build jobs and an 8 GiB memory limit. Environment
  preparation took 6m28s, ICU build/install 11m23s, PostgreSQL configure 2m39s,
  PostgreSQL core compilation 10m47s.
- The resulting amd64 PostgreSQL 16.15 executable started successfully in an
  isolated fresh ICU en-US database: both stored and actual collation versions
  were **153.112**. This is a source-prefix smoke test, not DEB installation evidence.
- Real extension compilation exposed old-source issues in jsquery and pg_cron.
  Both recipes now pin upstream fixes while preserving the managed SQL versions.
  The spqrhash recipe also declares python3-docutils, needed by its install target
  to generate bundled HTML documentation with rst2html.
  At that emulated-build checkpoint, the complete extension set and DEB installation
  qualification were still pending.
- The installed-wheel smoke check creates this named profile, validates its
  version/platform/private-ICU policy, checks all 38 catalog entries and the 34
  selected SQL versions, and verifies source pins against the catalog. The same
  check runs in the existing distribution CI job.

## Native Debian 13 amd64 package acceptance — 2026-11-03

The native amd64 build completed all 42 steps using 8 build jobs and a 30 GiB
container memory limit. The builder host runs Debian 12; build and installation
test containers run Debian 13. Packages are version `16.15-1.deb13`.

Fresh offline test containers passed separate libpq, client and server installation
scenarios. All 34 selected non-contrib SQL extensions were created at the declared
versions, including logerrors 2.1 and postgis_tiger_geocoder 3.4.0. Twelve functional
SQL probes passed, including PLV8 execution, pgRouting path finding, PostGIS raster,
H3, a TimescaleDB hypertable and vector distance.

The installed server loaded `libicudata.so.70.1`, `libicui18n.so.70.1` and
`libicuuc.so.70.1` from `/opt/test/yc16-icu70/16/lib`. An ICU en-US database reported
stored and actual collation version `153.112`; imported explicit ICU collations also
matched. Indexed and sequential queries agreed for 24,000 surname rows on Cyrillic
and Latin text, including straight and typographic apostrophes, pagination, ranges
and maximum values.

Lifecycle checks passed for startup/reload/restart, dump/restore, crash recovery,
page checksums, rejected invalid startup conditions, reinstall, removal and purge
with preserved PGDATA. The tests use an emulated systemctl in containers. Native
systemd, full upstream regression suites and physical replication from managed
PGDATA were not tested by this acceptance run.

The complete exported build is in `yc16/delivery` on the
builder. Installed-package evidence is in `yc16/package-acceptance`; local packages,
checksums and the acceptance report are in `work/native-build-20261103`.
