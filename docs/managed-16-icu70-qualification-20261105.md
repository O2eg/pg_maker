# Debian 13 amd64 qualification: PostgreSQL 16.15 yc-clone

The `yc-16.15-icu70` profile produced three `package-verified` DEBs on
2026-11-05 at 12:02:08 UTC. All 46 build steps succeeded and the final manifest
sets `publication_eligible: true`. This records the specific build below;
it does not grant qualification to subsequent builds.

| Item | Value |
| --- | --- |
| pg-maker | 0.3.0, commit `dfa21c80f23fc0a36f4d794aa88ba0554f6f2132` |
| Operation | `op-1236bcc287b347b888863370a3ecb13e` |
| Target | Debian 13 (trixie), native amd64 |
| Resources | 8 jobs, 30 GiB memory limit, 5 GiB free-disk reserve |
| Successful build and tests | 3082.52 seconds (51 minutes 23 seconds) |
| Test configuration | `profile: full`, `run_db_tests: true`, `standard` + `long` |
| Installed private ICU | 70.1; `en-US-x-icu` actual version `153.112` |

The duration excludes earlier failed/diagnostic attempts and environment preparation.
A server-side systemd controller ran the build independently of SSH. Build and
test containers used Debian 13; no database packages were installed on the host.

## Packages and provenance

All packages use Debian version `16.15-1.deb13` and architecture `amd64`.

| File | Bytes | SHA256 |
| --- | ---: | --- |
| `postgresql-16-yc-clone_16.15-1.deb13_amd64.deb` | 20361600 | `3329ccf5f943dff54d4cd2f0b7c6b0e631091117efd363a6c8ea9a11108198fe` |
| `postgresql-client-16-yc-clone_16.15-1.deb13_amd64.deb` | 704852 | `7595d3c952c61ca3e2ce1aa852c0b355bc4e4850a855c51ce0b03eed8cb2da65` |
| `libpq5-yc-clone_16.15-1.deb13_amd64.deb` | 126452 | `90916ae5282f605e6833bc065db022b92a088ec9b3bbb0d885106088fa19d147` |

The immutable delivery export contains the DEBs, source/installed test reports,
logs, diagnostics, resolved lock, execution plan and verification manifests.
All 6644 inventory entries and artifact/evidence hashes were reconciled.

| Record | Content hash |
| --- | --- |
| Lock | `sha256:0169296213d853a77941f320ad4ae468cf18d1856c143f56a108f19bfca83832` |
| Plan | `sha256:5f69ee4e6500cb7d1ed16c000ef6aef5ccf0153b15d38d4d1a384e9af2e97ea5` |
| Artifact manifest | `sha256:25073c5870acf4f2c8603c6317ae4b34ea77387078c0a1e32b669847e373956a` |
| Export tree | `sha256:3d2e728d7d4fa14fe474148034e2b8cea3f8be49deea4e8d62a74b25cc9aba12` |

The accompanying `QUALIFICATION.md`, `qualification-facts.json`,
`warning-review.json` and supplementary probe directories retain the detailed
result. Build outputs and test logs remain outside Git; this document identifies
them by hashes. No release tag was created.

## Source and extension tests

Core regression passed all 220 tests. `check-world` recorded 263 suites:
65 regression, 190 TAP and 8 isolation suites, including the upstream skips
listed below. SSL and WAL consistency checks were enabled. The long recovery
suite `027_stream_regress.pl` passed its nine assertions.

All 28 selected components passed their native suites: ICU, two contrib recipes
and 25 additional source recipes. These include:

| Component | Native result |
| --- | --- |
| ICU 70.1 | `make check` passed |
| pg_stat_query_plans | 12 regression scenarios passed |
| pg_partman | 12550 pgTAP assertions reported |
| pg_repack | 11 regression tests passed |
| pglogical | 28 regression tests; 6 TAP files / 181 assertions |
| pgvector | 14 regression tests; 45 TAP files / 1241 assertions |
| PostGIS | CUnit/XML checks and 331 + 331 SQL tests, including upgrade |
| pgRouting | 479 pgTAP files / 53711 assertions reported |
| TimescaleDB | Regression groups of 14/94/25/78/8 tests; 3 TAP files / 30 assertions |
| H3 | 3 CTest tests passed |

Native counts retain upstream TODO/SKIP semantics; they are not counts of
unconditionally executed successful assertions. The other selected components
also passed and are itemized in `qualification-facts.json`.

The pglogical resynchronization load test completed 548677 transactions with zero
transaction failures and matching full-table fingerprints after catch-up. Its
deterministic race regression passed all nine assertions. Runtime and test-fixture
changes are described in the [profile documentation](managed-16-icu70.md).

The utility itself passed 335 pytest tests; 35 environment-dependent integration
tests were skipped. Ruff, formatting, wheel/sdist validation and installed-wheel
profile initialization/validation passed. The wheel's 188 bundled files and the
remote project's 52 component/patch/catalog files matched the committed sources.

## Installed-package tests

Three separate fresh, offline Debian 13 containers installed dependencies from
the pinned local APT repository and tested the exact final DEBs:

| Package set | Result |
| --- | --- |
| libpq only | 7 checks passed |
| client + libpq | 34 checks passed |
| server + client + libpq | 3111 checks passed; previous-release upgrade marked N/A |

Checks covered inventory and ELF loading, dependencies, all 33 selected SQL
extension versions, SDK compilation/loading of an external C extension,
start/status/reload/restart, TCP and Unix sockets, dump/restore, crash recovery,
checksums, rejected invalid configuration/permissions/occupied ports, reinstall,
remove, install-after-remove and purge with data/configuration preservation.
PostgreSQL processes were real; the test harness emulated `systemctl`. Native
systemd PID1 lifecycle on a VM was outside this qualification.

The all-preload test exhausted the background-worker budget at the upstream
`max_worker_processes=8`: TimescaleDB logged a job-registration warning. A separate
fresh-container probe of these same DEBs used `max_worker_processes=32` and
executed a scheduled job with a real `User-Defined Action` worker, recording two
successful runs and zero failures. Allocate worker capacity for the extensions
and parallel queries actually enabled; the package does not change the default.

## Additional checks, skips and warning review

The libpq DNS load-balancing test, skipped in the main run without its external
fixture, passed all 125 assertions in a separate container with a private hosts
file. ICU checks compared 8896800 sort keys before/after the ICU-22779 backport:
all Unicode scalar values across four locales with numeric collation on/off,
plus practical strings and custom rules. The key streams and ICU data matched.
Final `postgres`, `psql`, `libpq` and all three ICU libraries were byte-identical
to the binaries used for these supplementary probes; see
`final-prefix-comparison.json`.

Remaining skips and limits are explicit:

- Upstream Windows-only SSPI and cloexec tests were skipped on Linux.
- PostgreSQL recovery test 031 retains its upstream instability skip.
- GSSAPI/LDAP/OAuth suites were not configured and are N/A in the inventory.
- pg_partman unsupported/not-yet-working cases, pglogical clock-change TODO/SKIP,
  pgRouting upstream TODOs and ICU known-issue suppressions remain recorded.
- No individual test was excluded under the one-hour rule.
- Previous-release package upgrade was not tested because no previous release
  was declared; current-version reinstall/remove/purge passed.
- PLV8 was disabled. Unavailable vendor variants remain explicitly excluded in
  the catalog; they were not silently substituted or marked tested.

The 375 source/build commands recorded 188 build warnings, 96 test warnings and
one test diagnostic; the installed server tests recorded two warnings and
11 diagnostics. The final warning review reconciled these with the logs, checked
extension backend symbols against `postgres`, and retained expected negative-test
messages. The worker-capacity issue above has separate runtime evidence. Older
ICU test sources still produce compiler diagnostics; a successful test run does
not imply zero warnings or prove the absence of every source defect.

This qualification covers this build and its packages. Physical replication from
the managed Yandex server, vendor binary identity, and compatibility of its
private modules were not tested here. Matching ICU collation versions alone does
not establish those properties.
