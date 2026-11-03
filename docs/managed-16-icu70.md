# PostgreSQL 16.15 with private ICU 70.1

This profile targets Debian 13 / x86_64 / DEB. It builds upstream PostgreSQL 16.15
with source-pinned ICU 70.1, installs the required ICU runtime libraries inside the
product prefix and selects public-source extensions from the managed catalog.
It does not reproduce Yandex patches or claim vendor binary compatibility.

## Create and build

```bash
pg-maker init yc16 --profile yc-16.15-icu70
pg-maker validate yc16/build.yaml
pg-maker config show yc16/build.yaml --resolved
pg-maker env prepare yc16/build.yaml --out yc16/environments.json --operations yc16/operations
pg-maker resolve yc16/build.yaml --environments yc16/environments.json --out yc16/lock.json --input-store yc16/inputs --operations yc16/operations
pg-maker plan yc16/lock.json --goal package --out yc16/plan.json
pg-maker build yc16/lock.json --plan yc16/plan.json --operations yc16/operations --detach
```

Run as the non-root UID/GID in `config/defaults.yaml` (default `1000:1000`). All
project files remain editable. The standard `init` still creates the 18.1 profile.
Reserve substantially more than the configured 20 GiB minimum for the complete
extension set, especially PostGIS/GDAL and PLV8/V8, inputs and checkpoints.
No full amd64 distribution build has yet qualified this new profile. Each selected
extension is marked `selected-unverified`, never implicitly certified by metadata.

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

## Pinned library mechanism

`components/libraries/icu70.yaml` pins the upstream ICU 70.1 source archive by SHA256.
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
their versioned targets. Development headers and unselected ICU tools are excluded.
RPATH is checked by the existing ELF/package validator. Installing a package never
initializes or starts a database automatically.

The mechanism accepts other source-pinned Make/Autotools libraries with private
pkg-config files. A provider must correspond to an enabled PostgreSQL option;
multiple providers for the same option or dependencies on post-core components fail.
It does not infer unspecified dependency versions or enable network access in builds.

## Managed extension coverage

The source inventory is the 2026-11-02 non-contrib catalog snapshot from
`rc1d-kp80p78vpf7pi1pd.mdb.yandexcloud.net`, database `ad_analytics`.
Only pg_repack was installed in that database; the list describes availability.
The editable [catalog](../src/pg_maker/bundled/catalogs/yc-16.15-extensions.yaml)
retains all 38 names, default/installed/catalog versions, source pins, selection and
exclusion reasons. The profile selects 34 SQL extensions through 26 source recipes,
plus contrib pg_stat_statements and ICU. No missing component is silently substituted.

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
with FetchContent downloads disabled. Git submodules, including PLV8's inputs, are
frozen by the existing resolver before compilation.

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

PostgreSQL 16's full database-test evidence adapter remains unavailable, so the
profile builds/packages in quick mode and reports unverified. Pre-core libraries and
CMake components also reject full qualification rather than marking skipped tests
as passed. Instructions to run the new profile are not evidence that all 34 extensions
have compiled, loaded or passed application queries on this platform.

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
  The complete extension set and DEB installation qualification remain pending;
  no full package is certified by these partial results.
- The installed-wheel smoke check creates this named profile, validates its
  version/platform/private-ICU policy, checks all 38 catalog entries and the 34
  selected SQL versions, and verifies source pins against the catalog. The same
  check runs in the existing distribution CI job.
