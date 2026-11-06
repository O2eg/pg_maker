# PostgreSQL 17 managed-compatibility profile

`yc-17.11-icu70` targets **Debian 13 amd64**, community **PostgreSQL 17.11**,
private **ICU 70.1**, and the available SQL extension versions audited on
`rc1a-j8rnpvd41jpbqmrb.mdb.yandexcloud.net` (`10.237.32.28`) on 2026-11-06.
The profile is **prepared, pending native build and package qualification**.
Its component recipes are independent of the qualified PG16 and PG18 profiles.

```bash
pg-maker init yc17 --profile yc-17.11-icu70
pg-maker validate yc17/build.yaml
pg-maker env prepare yc17/build.yaml --out yc17/environments.json --operations yc17/operations
pg-maker resolve yc17/build.yaml --environments yc17/environments.json --out yc17/lock.json --input-store yc17/inputs --operations yc17/operations
pg-maker plan yc17/lock.json --goal package --out yc17/plan.json
pg-maker build yc17/lock.json --plan yc17/plan.json --operations yc17/operations --detach
```

## Observed inputs

| Input | Managed observation | Profile |
| --- | --- | --- |
| PostgreSQL | `17.11-201-yandex.60286.5a0f49380c` | Community 17.11, official source SHA-256 pinned |
| Compiler | GCC 11.4.0, Ubuntu package `11.4.0-1ubuntu1~22.04.3` | Debian 13 toolchain |
| Architecture | x86_64 | x86-64 core baseline; vectorscale requires AVX2/FMA |
| Database | `db1`, UTF-8, ICU `en-US`, LC_COLLATE/LC_CTYPE `C` | Private ICU; installed ICU-database checks |
| en-US stored / actual collation version | `153.112` / `153.112` | Both must equal `153.112` |
| ru-RU stored / actual collation version | `153.112.40` / `153.112.40` | Both must equal `153.112.40` |
| ICU Unicode data | `14.0` | ICU 70.1 Unicode 14.0 |
| Exact managed libicu package | Not exposed by the SQL audit | **Unconfirmed**; 70.1 is the selected matching candidate |
| libc collation version | `2.27` | Debian 13 libc, not matched by private ICU |
| Checksums | Enabled | Physical copies preserve the source setting |
| Block / WAL block / WAL segment | 8192 / 8192 / 16 MiB | Upstream defaults |
| Extensions installed in db1 | Only `plpgsql` | Inventory below describes availability, not use |
| `pg_config`, `shared_preload_libraries` | Permission denied to audit role | Recipe preloads are build requirements; managed preload inventory is unknown |

Matching SQL extension and ICU collation versions does not establish vendor
binary equivalence or physical replication compatibility. Vendor patches,
extension storage formats and libc collations need separate verification.
No source credentials are included in the profile.

The package bundles `libicudata.so.70`, `libicui18n.so.70` and `libicuuc.so.70`
under its private prefix. It does not require the OS to supply ICU 70.1.
Installed-package checks verify database and collation stored/actual versions
and reject mismatches. LZ4 and Zstandard are enabled.

| Role | Expected package filename |
| --- | --- |
| Server and extensions | `postgresql-17-yc-clone_17.11-1.deb13_amd64.deb` |
| Client | `postgresql-client-17-yc-clone_17.11-1.deb13_amd64.deb` |
| libpq ABI 5 | `libpq5-yc-clone_17.11-1.deb13_amd64.deb` |

## Sources and exclusions

The editable `catalogs/yc-17.11-extensions.yaml` retains all **44** non-contrib
names. **39** names are selected and their installed SQL versions are asserted.
PLV8 remains commented out. The optional `repl_mon` preload module is not selected:
its presence on this source was not observable with the audit role.

AGE uses public PG17 revision `dfd1e1bc7c98b76b646837496f3762a6bc4d5184`, after
the PG17 port and before the version bump to 1.6.0. It declares SQL version
1.5.0; there is no matching official PG17 1.5.0 release tag. Orafce's public
`VERSION_4_14_6` tag declares SQL version 4.15. These are source choices, not
claims of identical managed patches. H3 4.1.3 uses its pinned H3 core 4.1.0.

Shared fixes from the PG18 profile are retained only where applicable to the
pinned sources: pg_stat_query_plans permissions/uint64 formatting, pglogical
idle/resync handling, pg_partman calendar fixtures, vector CPU flags,
pg_textsearch scoring/test fixes, and TimescaleDB archive/test configuration.
PG17.11 already has `output_plugin_libraries`; pglogical and TimescaleDB test
clusters must configure it. The pglogical 2.4.5 Perl fixtures have their own
patch. PG18's smlar accessor compatibility patch is not needed on PG17.

The vectorscale helper selects the actual PostgreSQL major via `pg_config` for
both compilation and tests. Version 0.9.0 shares the frozen Rust 1.91.1,
cargo-pgrx 0.16.1 and 621 Cargo archives with the PG18 profile. Cargo runs offline.
The upstream source ignores ten historical upgrade tests and returns early on
PG17. They are recorded as **upstream-disabled, not executed or passed**; this
does not establish historical PG17 upgrade coverage. Unexpected ignored tests
fail the gate. Production artifacts are restored after the test build.

The existing explicit operator exception for RUM `t/002_pglist.pl` is retained
with its pinned test-file hash and exact missing-fixture skip message. Other
RUM SQL, isolation and WAL tests remain mandatory. The exception does not count
as a passing test. License inventory remains explicit, including unconfirmed
license files for some public vendor modules and TimescaleDB's TSL component.

## Qualification policy

Standard PostgreSQL, contrib and extension tests run by default. Package tests
run in fresh Debian 13 containers. To include the expensive core suites in the
same locked package operation, set this before preparing/resolving:

```yaml
spec:
  validation:
    profile: full
    baseline: false
    test_suites: [standard, long]
    run_db_tests: true
```

The separate command is `test-full`. A test exceeding the operator-approved
one-hour limit may be excluded only with its identity, elapsed time, evidence
and reason retained. A timeout must not conceal compiler/assertion failures.
Do not count configuration validation or Python utility tests as database
qualification. Keep all three PG16/PG18 DEBs, checksums and qualified delivery
archives when cleaning completed build workspaces.

## Available non-contrib SQL versions

Selected entries below still require native qualification. Blocked entries stay
in the catalog; they are not silently replaced by a differently versioned library.

| Extension | Managed default SQL version | Profile selection |
| --- | --- | --- |
| `address_standardizer` | `3.4.3` | Selected, native qualification pending |
| `address_standardizer_data_us` | `3.4.3` | Selected, native qualification pending |
| `age` | `1.5.0` | Selected, native qualification pending |
| `anon` | `1.3.2.1` | Exact managed SQL version 1.3.2.1 is not present in the confirmed public upstream 1.3.2 source. |
| `h3` | `4.1.3` | Selected, native qualification pending |
| `h3_postgis` | `4.1.3` | Selected, native qualification pending |
| `hypopg` | `1.4.1` | Selected, native qualification pending |
| `ip4r` | `2.4` | Selected, native qualification pending |
| `jsquery` | `1.1` | Selected, native qualification pending |
| `logerrors` | `2.1` | Selected, native qualification pending |
| `lwaldump` | `1.0` | Selected, native qualification pending |
| `oracle_fdw` | `1.2` | Requires an explicitly provisioned proprietary Oracle Instant Client SDK and runtime. |
| `orafce` | `4.15` | Selected, native qualification pending |
| `pg_clickhouse` | `0.3` | Selected, native qualification pending |
| `pg_cron` | `1.6` | Selected, native qualification pending |
| `pg_hint_plan` | `1.7.0` | Selected, native qualification pending |
| `pg_partman` | `5.4.3` | Selected, native qualification pending |
| `pg_qualstats` | `2.1.1` | Selected, native qualification pending |
| `pg_repack` | `1.5.3` | Selected, native qualification pending |
| `pg_stat_kcache` | `2.3.1` | Selected, native qualification pending |
| `pg_stat_query_plans` | `1.0` | Selected, native qualification pending |
| `pg_textsearch` | `1.3.0` | Selected, native qualification pending |
| `pg_tm_aux` | `1.1.1` | Selected, native qualification pending |
| `pg_uuidv7` | `1.5` | Selected, native qualification pending |
| `pgaudit` | `17.0` | Selected, native qualification pending |
| `pglogical` | `2.4.5` | Selected, native qualification pending |
| `pglogical_origin` | `1.0.0` | Selected, native qualification pending |
| `pgrouting` | `3.6.2` | Selected, native qualification pending |
| `pgvector` | `0.8.2` | Unconfirmed managed alias; upstream v0.8.2 provides vector.control only. |
| `plv8` | `3.2.3` | Disabled by user request; leave the feature entry commented out. |
| `postgis` | `3.4.3` | Selected, native qualification pending |
| `postgis_raster` | `3.4.3` | Selected, native qualification pending |
| `postgis_sfcgal` | `3.4.3` | Selected, native qualification pending |
| `postgis_tiger_geocoder` | `3.4.3` | Selected, native qualification pending |
| `postgis_topology` | `3.4.3` | Selected, native qualification pending |
| `prefix` | `1.2.0` | Selected, native qualification pending |
| `rum` | `1.3` | Selected, native qualification pending |
| `sa_mon` | `1.0` | No matching public source found in the source audit. |
| `smlar` | `1.0` | Selected, native qualification pending |
| `spqrguard` | `2.4.2` | Selected, native qualification pending |
| `spqrhash` | `1.2` | Selected, native qualification pending |
| `timescaledb` | `2.23.0` | Selected, native qualification pending |
| `vector` | `0.8.2` | Selected, native qualification pending |
| `vectorscale` | `0.9.0` | Selected, native qualification pending |
