# Changelog

## 0.6.0 — 2026-11-11

- Add read-only runtime preflight for planned PostgreSQL builds through
  `runtime check`, including machine capabilities for pg-play integration.

- Publish tested DEB/RPM artifacts to configured Nexus APT/YUM/Raw and filesystem
  receivers, directly or automatically after builds. Add durable publication workers,
  package-test and checksum gates, request deduplication, remote reconciliation and
  native index verification. Credentials remain external references.
- Bind publication retries to the tested artifact plan, reject incomplete exported
  build matrices, and reconcile interrupted HTTP response reads without reuploading.

- Add standalone extension projects with packaged PostgreSQL SDK targets, independent
  native package versions, DEB/RPM dependencies, and isolated installation checks.
- Include pg_wait_sampling, pg_stat_kcache and pg_repack as initial packaging cases.
- Provide an explicit, non-destructive migration of bundled kcache/repack files into
  separate packages while retaining the full standard contrib inventory.
- Correct synchronous extension CLI outcomes and idempotent request IDs; bind SDK
  caches and native migration versions to the selected modules and original inputs.
- Install runtime dependencies in extension SDKs, clean owned test tablespaces on
  retry, and preserve live command output through timeouts and cancellation.
- Fix documentation resume in retained workspaces by clearing partial render output
  and preserving previous diagnostic evidence. Update integration fixtures for full
  contrib defaults, component staging, workspace reuse and checkpoint retention.

## 0.5.0 — 2026-11-10

- Include the full standard PostgreSQL contrib by default, including optional
  modules and auto_explain. Check its install inventory independently of the
  managed extension catalog and include it in the server DEB.
- Add yc-15.18-icu70, yc-15.19-icu70, yc-16.14-icu70, yc-17.10-icu70 and
  yc-18.4-icu70 profiles. Keep private ICU 70.1, PostgreSQL-style versions,
  revision 1 and disabled PLV8. Record unavailable vendor sources and unverified
  physical compatibility explicitly for every managed profile.
- Resume builds in retained component workspaces with input/dependency receipts.
  Stage each component installation separately, track owned files, remove retired
  outputs and recover interrupted installs. Preserve unchanged objects and rebuild
  affected components and dependents.
- Add final/milestones/all checkpoint policies and workspace retention controls.
  Reuse prepared environments and validated Rust toolchains, avoid repeated source
  hashing within a process, and omit test/documentation inputs when disabled.
- Allow explicit package-only validation with run_db_tests: false and
  run_package_tests: true. Require successful installed-package checks without
  granting source qualification; ordinary builds still run source tests by default.
- Adapt PostgreSQL 15 test support and managed extension recipes to Debian 13.
  Backport the PostGIS 3.3.3 GIDX accessors to 3.3.2 and compare GiST, SP-GiST and
  BRIN results with sequential scans for 2D/3D/4D geometries in package checks.
- Remove environment-specific database addresses and workstation paths from public
  documentation/catalogs; keep credentials, build outputs and operational reports
  outside Git and Python distributions.

Validation: seven Debian 13 amd64 profile builds produced 21 DEBs and passed
separate libpq/client/server checks in fresh containers. Source database suites
were explicitly skipped for those builds; vendor physical compatibility remains
unverified. The Python utility passed 444 unit/contract tests, two Docker workspace
recovery tests, Ruff, wheel/sdist content checks and isolated wheel installation
with all eight bundled YC profiles.

## 0.4.1 — 2026-11-06

- Fix the offline package-install integration fixture: supply the required
  diagnostic stage and read only numbered command receipts. Verify diagnostic
  coverage for each installed package set.

## 0.4.0 — 2026-11-06

- Fix Python 3.10 imports in the vectorscale helper; install the conditional
  `tomli` runtime dependency without requiring dev extras. Smoke-test wheel
  installation on Python 3.10 and 3.12.
- Give all YC profiles `/opt/postgresql-yc-clone/<major>` binaries,
  `/var/lib/postgresql-yc-clone/<major>/data` and a matching service name.
  Preserve separate feature paths for the generic profile. Normalize PostgreSQL
  private subdirectories when a vendor prefix contains `postgres` or `pgsql`.
- Update pip-shipped manuals and generated project instructions, including explicit
  skipped-test builds, package locations, Nexus/APT steps and ARM64 limitations.
- Complete the main README build/export workflow and documentation index; align
  test adapters, naming policy, API references and historical-report scope with
  current behavior.
- Add the Debian 13 amd64 `yc-17.11-icu70` profile with private ICU 70.1,
  the audited PG17 extension catalog and explicit blocked/disabled entries.
  Keep PLV8 commented out. Native core/extension and fresh-container package
  qualification completed for the locked /opt/test/yc17-icu70/17 layout,
  with explicit RUM and upstream-test limitations.
- Adapt pg_stat_query_plans utility tracking and extended-protocol tests for PG17;
  retain native MERGE RETURNING coverage. Use exact-offset pglogical sync patches
  and synchronize replicated DDL in its add_table fixture.
- Select the actual PostgreSQL 17/18 major in the offline vectorscale helper
  and report upstream-disabled PG17 upgrade tests without counting them as passed.

## 0.3.0 — 2026-11-05

- Add scoped, ordered diagnostic rules with recorded exceptions and standalone
  `logs verify`; enforce policies in command, generated-test and package-install logs.
  Reject tool selectors for generated logs, whose executable identity is unknown.
- Apply locked standard/long test-suite policy and additional required test IDs to
  package goals; require matching source-policy evidence before qualification.
- Enable PostgreSQL 17 regression/TAP evidence and version-specific long suites,
  including the mandatory libpq_encryption flag.
- Add generic server_dev_files packaging policy, enabled in the managed profile,
  delivering headers/PGXS/support libraries in the server package and testing an
  external extension against the installed SDK without a separate devel package.
  Exclude ECPG pkg-config metadata together with its libraries and reflect SDK
  inclusion in the build plan.

Validation: 331 unit/contract tests and the Docker diagnostic-policy fixture passed.
The PostgreSQL 17.5 rerun passed core regression and reached check-world before it
was cancelled for this commit. Complete source/package/installed-SDK qualification
of these changes remains pending.

- Enable source/component tests by default for every profile, and fresh-container
  installation checks for normal package builds. Explicit skipped-test development
  builds remain unverified. Add `test-full` for expensive core suites.
- Add PostgreSQL 16 regression evidence parsing and explicit native test commands for
  ICU, CMake and managed extension recipes, with nonempty result checks.
- Inventory command warnings/errors, retain generated test logs and block failing
  stages or dangerous diagnostics. Optional warnings_as_errors tightens build policy.
- Check all declared installed SQL extension versions and fix the live socket-lock
  fixture for PostgreSQL 16. The new DB suites await an authorized native build.

- Add per-role package-name templates and manifest naming overrides. The managed
  profile now selects postgresql-16-yc-clone, postgresql-client-16-yc-clone and
  libpq5-yc-clone; finished DEBs keep their original names until rebuilt.
- Disable PLV8 by commenting out its managed-profile entry; retain the pinned
  recipe and catalog entry with an explicit disabled status. New projects select
  33 SQL extensions from 25 managed recipes. Existing packages are unchanged.
- Locked component source patches with immutable original/patched source trees and
  offline patch inputs.
- PLV8 3.2.3: include the standard algorithm declarations required by GCC 14, complete
  interrupted V8 builds, and use the parent make jobserver for nested compilation.
- pgRouting 3.5.0: remove an invalid Datum conversion of SPI_getvalue's text pointer
  so the pinned release builds with Debian 13's GCC 14.
- PostGIS 3.4.0: require its actual postgis_raster-3.so runtime library when packaging.
- Install the logerrors 2.0-to-2.1 SQL upgrade path and include contrib fuzzystrmatch,
  required by postgis_tiger_geocoder, in the managed profile.
- Native Debian 13 amd64 packages passed isolated installation/lifecycle checks,
  creation of all 34 selected extensions and ICU 70.1 collation checks (153.112).

## 0.2.0 — 2026-11-03

- Select the requested architecture from Docker multi-platform image stores during
  environment preparation, including amd64 builds under ARM emulation.
- Configurable two-part PostgreSQL or three-part product versions; package revisions
  remain independent.
- Source-pinned libraries before PostgreSQL configure, exact private pkg-config checks,
  runtime delivery and core/cache invalidation on library changes.
- CMake components, locked auxiliary sources and installed extension-version checks.
- Debian 13 PostgreSQL 16.15 / ICU 70.1 init profile with a complete 38-name managed
  extension catalog, 34 selected SQL extensions from 26 recipes and four explicit
  exclusions.
- Upstream PostgreSQL 16 build fixes for jsquery and pg_cron without changing their
  SQL versions, and the docutils build dependency required by spqrhash installation.
- Public GitHub SSH submodules resolve over HTTPS within public HTTPS checkouts.
- Installed-wheel coverage for the complete named profile, including source pins
  and managed SQL versions. Local build inputs and outputs are excluded from Git.

The amd64 server built under QEMU reports ICU collation version 153.112. The full
extension set and final DEB installation are still being qualified; selected catalog
entries retain their unverified status.

## 0.1.0 — 2026-10-18

Initial release of the PostgreSQL distribution controller:

- Strict YAML configuration, pinned sources/environments, immutable locks and plans.
- Isolated Docker builds, durable operations, events, cancellation, recovery and caches.
- PostgreSQL 14–19 catalog; initial Debian 13 / x86_64 / DEB platform.
- Default pg_stat_statements, pg_stat_kcache and pg_wait_sampling recipes with manuals.
- Documentation-only and combined builds using DocBook/Sphinx, local assets, HTML/man/PDF
  and explicit translation bundles.
- Server/client/libpq/docs packaging, manifests, offline installation checks and exports.
- Owned Git development sessions, patch import/refresh/pop/push/rebase, conflict recovery,
  selected component rebuilds, debug commands and iteration retention.
- Optional pg_play CLI/API/MCP integration and runtime image handoff.
- English project documentation, distribution-content checks and a tag-triggered PyPI
  Trusted Publishing workflow.

Database suites are opt-in; quick/development builds remain unverified. RPM, additional
platforms and PostgreSQL distribution repository publication remain future work.
See the [roadmap](docs/roadmap.md) and dated validation reports for precise boundaries.
