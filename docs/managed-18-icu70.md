# PostgreSQL 18 managed-compatibility profile

`yc-18.6-icu70` builds community PostgreSQL **18.6** for **Debian 13 amd64**,
with private **ICU 70.1** and the public extension versions observed on
`rc1a-novptt4hkfpbodb6.mdb.yandexcloud.net` on 2026-11-05. It has independent
component recipes; the qualified PostgreSQL 16 profile remains unchanged.

```bash
pg-maker init yc18 --profile yc-18.6-icu70
pg-maker validate yc18/build.yaml
pg-maker env prepare yc18/build.yaml --out yc18/environments.json --operations yc18/operations
pg-maker resolve yc18/build.yaml --environments yc18/environments.json --out yc18/lock.json --input-store yc18/inputs --operations yc18/operations
pg-maker plan yc18/lock.json --goal package --out yc18/plan.json
pg-maker build yc18/lock.json --plan yc18/plan.json --operations yc18/operations --detach
```

The resulting package names are:

| Role | Filename |
| --- | --- |
| Server and extensions | `postgresql-18-yc-clone_18.6-1.deb13_amd64.deb` |
| Client | `postgresql-client-18-yc-clone_18.6-1.deb13_amd64.deb` |
| Client library | `libpq5-yc-clone_18.6-1.deb13_amd64.deb` |

The PostgreSQL version format has two components. `libpq5` follows the upstream
ABI naming convention and intentionally has no PostgreSQL major in its name.
ICU is installed under the distribution's private prefix and travels with the
server package; the package does not request the host's ICU 70 package.
LZ4 and Zstandard are enabled, including support for the source's LZ4 TOAST/WAL.

## Managed observations and limits

| Input | Managed observation | Profile |
| --- | --- | --- |
| PostgreSQL | `18.6-201-yandex.62650.c500aa0389` | Community 18.6 |
| Architecture | x86_64 | x86-64 baseline for core; vectorscale additionally requires AVX2/FMA; amd64 packages |
| Database ICU locale | `en-US`, UTF-8 | Private ICU 70.1 |
| en-US stored / actual collation | `153.112` / `153.112` | Expected `153.112` |
| ru-RU actual collation | `153.112.40` | Expected `153.112.40` |
| ICU Unicode data | `14.0` | ICU 70.1 Unicode 14.0 |
| Exact managed libicu package | Not exposed to the audit role | **Not confirmed** |
| libc en_US.utf8 collation | `2.35` | Debian 13 libc; not matched by private ICU |
| Data checksums | Enabled | Physical copies preserve the source setting |
| Block / WAL block / WAL segment | 8192 / 8192 / 16 MiB | Upstream defaults |

Matching extension SQL versions and ICU collation versions does not establish
vendor binary equivalence or qualify a physical replica. Vendor core patches,
extension patches, on-disk formats and libc collations still need separate
verification. No managed source credentials are included in the project.

## Extension inventory

The editable `catalogs/yc-18.6-extensions.yaml` retains all **44** non-contrib
names. **39** are selected for compilation and testing. Each selected recipe
asserts the installed SQL version from the managed catalog.

| Excluded name | Reason |
| --- | --- |
| `plv8` 3.2.4 | Explicitly disabled; its feature entry remains commented out |
| `anon` 1.3.2.1 | Public 1.3.2 does not establish the vendor's additional `.1` patch |
| `sa_mon` 1.0 | Matching public source not found |
| `pgvector` 0.8.2 | Unconfirmed managed alias; upstream provides `vector`, which is selected |
| `oracle_fdw` 1.2 | Requires a separately provisioned Oracle Instant Client SDK/runtime |

`repl_mon` is additionally selected as a preload module without an SQL extension.
The source did not report its module version. Public source availability is
recorded separately from license status: the audit did not find explicit license
files for `lwaldump`, `smlar` and `spqrguard`; TimescaleDB includes TSL code.

## Frozen Rust inputs and test fixtures

`vectorscale` 0.9.0 uses Rust 1.91.1 and cargo-pgrx 0.16.1. The recipe pins the
toolchain archives (including rustfmt, required by pgrx bindgen) and a generated
Cargo.lock patch. A separate YAML catalog
pins 621 crate archives with their Cargo checksums. `extra_source_catalogs`
merges these into ordinary `extra_sources` before locking; duplicate keys fail
validation. The build runs Cargo offline against these inputs. Rust tools and
crate sources are build inputs and are not copied into the database packages.

On x86-64, upstream vectorscale requires AVX2 and FMA at compile time and checks
those CPU features when loaded. Its library and native tests use explicit
`-Ctarget-cpu=x86-64 -Ctarget-feature=+avx2,+fma`; build tools and PostgreSQL core
retain the baseline. The build VM exposes both features. Deployment CPUs must
also expose them to use vectorscale.

Explicit `build_commands` support the pgrx build/install steps. They are argv
arrays, are included in reuse identity, and cannot be combined with Make or
CMake build actions. Standard native-test and installed-version gates still
apply. The production release library is restored after pgrx's test build. The helper
sets pgrx's `USER` to the actual container OS account, matching its initdb role.
The pinned source disables ten historical-release upgrade tests and explicitly
returns without testing on PG18. Their exact inventory is recorded as inapplicable,
not passed; any additional ignored Rust test fails qualification.

The profile also exercises pg_partman's role/procedure/BGW fixtures and checks
`lwaldump` on a temporary physical standby, including an abrupt restart.

## Compatibility changes found during native testing

The source hashes and patch files are part of the resolved build inputs. An
extension's SQL version alone does not identify the compiled implementation.

| Component | Adjustment |
| --- | --- |
| `jsquery` 1.1 | Pin public revision `f43eabd5c5dad311894fb131a62999928743c726` with PostgreSQL 18 API support |
| `logerrors` 2.1 | Pin public `fix_pg_18` revision `c72bbe833c216814495cd9b4d32270dd18dd253b`; run its regression and error-capture checks |
| `pg_stat_query_plans` 1.0 | Restore privileged access to other users' plans; also test that ordinary users cannot read those plans; use PostgreSQL's portable uint64 log format |
| `vector` 0.8.2 | Remove the upstream `-march=native` override so binaries retain the profile's x86-64 baseline |
| `prefix` 1.2.0 | Pin public revision `413f00607c3c3446e4f804ef1874a98caefabd4d` with current PostgreSQL APIs; the old 1.2.0 tag only supported PG8/9 |
| `smlar` 1.0 | Use PostgreSQL 18's inline `TupleDescAttr` accessor instead of shadowing it with the legacy compatibility macro |
| `pg_hint_plan` 1.8.0 | Run native expected-output tests with the upstream preload configuration; combined preloads are checked separately during package acceptance |
| `pglogical` 2.4.6 | Port test fixtures to current PostgreSQL Perl/WAL APIs and explicit output-plugin policy; finish idle tablesync after received WAL progress and refresh the resync cache after waiting for a relation lock; compare replicated table contents and test the lock race |
| `pg_partman` 5.4.3 | Correct 30-second calendar fixtures and derive monthly undo counts from generated input; compare all copied payloads |
| `pg_textsearch` 1.3.0 | Run shell suites from a short temporary path, set writable test socket directories and preload the library in the stress cluster and fail on client/server SQL errors; backport upstream #405 to protect standalone scoring against concurrent segment recycling, with its dedicated regression test; force replication search/count sessions to exercise BM25 indexes, assert the UPDATE query plan, and count matching fixture documents; assert exact segment-match counts using negative BM25 scores without an arbitrary magnitude cutoff; require positive evidence that TimescaleDB integration scenarios executed rather than silently skipped |
| `timescaledb` 2.23.0 | Provide pinned archive commit metadata, allow the invalidation output plugin in test clusters, and include/check that plugin in the installed package |

The pg_textsearch scoring fix is backported from upstream commit
[`dcbb8063564374c9fdfec73445086694cb2b2703`](https://github.com/timescale/pg_textsearch/commit/dcbb8063564374c9fdfec73445086694cb2b2703)
([PR #405](https://github.com/timescale/pg_textsearch/pull/405)). Native stress
exposed `invalid segment header` errors which the original stress script printed
but did not propagate to its exit code. The fixture now rejects failing readers
or writers and server ERROR/PANIC records, and preserves logs before cleanup.
The backport changes locking, not the SQL version or index disk format.
Qualification still requires the full native suites to pass.

This backport does not address every upstream 1.3.0 replication issue. Upstream
[PR #406](https://github.com/timescale/pg_textsearch/pull/406) addresses reuse of
segment pages still read by a hot standby, but introduces a v8 metapage and
requires a standby-feedback horizon. It is not included in this profile: the
managed extension's actual index format and vendor patches have not been
established. Passing the bundled replication scenarios does not establish that
broader compatibility. Qualify existing managed BM25 indexes separately before
using this package as their physical standby.

The pg_partman helper follows emitted pgTAP procedure instructions, creates a
separate database for its mixed-case extension schema, and runs clock-sensitive
fixtures with a private process time offset. Host time and installed SQL functions
are unchanged. Upstream's `not_working_yet` directory is inventoried as inapplicable,
not counted as passed.

The independent PG18 ICU recipe checks installed `en-US-x-icu` and `ru-RU-x-icu`
stored/actual versions, rejects any mismatched ICU collation, and creates an
`en-US` ICU database to check its stored/actual `153.112` version. These checks
run against the installed DEBs in the package acceptance container.

The upstream pglogical daylight-saving-time scenario `t/030` remains its original
`TODO/SKIP`: it requires rewriting privileged host-clock changes. Core scenarios
requiring disabled configure features, such as injection points, are likewise
not applicable to this build. Neither category represents executed passing tests.

PostgreSQL 18.6 restricts logical decoding plugins through `output_plugin_libraries`.
To use pglogical or TimescaleDB's WAL invalidation mode, a deployment needs
`wal_level = logical` and an appropriate allowlist, for example:

```conf
output_plugin_libraries = 'pgoutput, test_decoding, pglogical_output, timescaledb-invalidations-2.23.0'
```

The package includes `timescaledb-invalidations-2.23.0.so` and checks that it can
load in the fresh package-test container. Native continuous-aggregate tests verify
its WAL behavior with the required settings.

PostGIS uses Debian 13's GEOS 3.13.1, GDAL 3.10.3, SFCGAL 2.0.0 and PROJ 9.6.0.
GEOS 3.14-specific features are not available; the managed instance's GEOS version
was not established by the SQL-extension inventory audit.

RUM's optional `t/002_pglist.pl` requires the original approximately 1 GB mailing
list dump. At the initial native run its upstream download host was unavailable.
On 2026-11-05 the operator explicitly approved excluding only `t/002_pglist.pl`.
The recipe pins that file's SHA-256 and exact upstream skip-all message. The
report lists the exclusion, its reason and approval; it does not count that
script as passed. SQL, isolation and WAL tests remain mandatory. A changed file,
missing TAP result, different skip reason or actual test failure still fails the
build. This exception does not establish coverage of the unavailable large-data
fixture.

## Full qualification and preserving previous releases

Standard source and extension tests are enabled by default. For a package run
that must also include the expensive core suites, set the following **before**
preparing and resolving the project:

```yaml
spec:
  validation:
    profile: full
    baseline: false
    test_suites: [standard, long]
    run_db_tests: true
```

`test-full` is the standalone command for the expensive source suites. Package
acceptance runs in fresh containers of the target OS. Qualification requires
successful source, extension and package gates, plus review of diagnostic logs.
An operator-approved one-hour test exclusion must retain the timed-out test's
identity, elapsed time and logs; it must not hide an assertion or compiler failure.

Use a separate project, operation directory and delivery directory for each major.
Before a new major build, copy all three previous DEBs, SHA256SUMS and the complete
qualified delivery archive to a release directory, then verify their checksums.
Do not clean that directory while preparing or retrying the new build.

This profile's initial status is **selected, pending native qualification**.
The completed operation's reports, rather than the existence of a recipe, establish
whether the resulting packages passed qualification.
