# PostgreSQL 15 YC clone candidate profiles

`yc-15.18-icu70` and `yc-15.19-icu70` target Debian 13 amd64 with private ICU 70.1,
package revision 1, full standard contrib and PLV8 disabled. They share one audited
managed extension inventory. These profiles are prepared candidates, not a
qualified physical replica distribution. See the [compatibility contract](yc-clone-compatibility.md).

## Observed managed source

Read-only SQL inspection on 2026-11-09 used TLS verify-full on
a managed PostgreSQL 15 reference database.
The server reports PostgreSQL **15.19 (Ubuntu 15.19-201-yandex.55987.e48409f053)**,
x86_64, GCC 11.4.0. The connected server is primary. Only plpgsql 1.0 is installed
in that database; the inventory below describes available extensions, not installed objects.

Database encoding is UTF8, default provider libc, LC_COLLATE/LC_CTYPE C/C.
There are 787 ICU collations: 460 report stored/actual `153.112/153.112`, 327 report
`153.112.40/153.112.40`. Root/English use the first version; Russian uses the second.
No stored/actual mismatch was observed. Exact managed libicu package/version is
**unconfirmed**: pg_config and pg_read_file are denied, and no ICU version function
is exposed. ICU 70.1 is the previously tested matching candidate, not a direct
measurement of the managed library package. A collation version is not a libicu version.

Page/WAL page size is 8192 bytes, WAL segments are 16 MiB, checksums are enabled,
and default TOAST/WAL compression is LZ4. The 15.18 profile deliberately uses an
older minor than this observed server; it does not inherit a 15.19 qualification.

## Coverage and unresolved compatibility differences

All 36 non-contrib SQL extensions are recorded. 32 are selected from public pinned
sources; PLV8 is explicitly disabled. Three remain blocked: exact `anon 1.3.2.1`
source, proprietary Oracle client dependencies for `oracle_fdw`, and the managed
`pgvector` alias (upstream `vector 0.8.2` is selected under its genuine name).
A blocked entry is a missing requirement, not an authorized exclusion or a claim
that the clone has the complete managed extension set.

The full upstream contrib is always built and packaged, including auto_explain,
UUID, SELinux and procedural-language transform modules. Its independent file
inventory prevents missing contrib files from passing packaging checks. The
external catalog does not filter the standard contrib payload.

Two managed contrib versions differ from upstream 15.19:

| Extension | Managed | Upstream 15.19 | Status |
| --- | --- | --- | --- |
| amcheck | 1.4 | 1.3 | Vendor backport/source not obtained |
| pageinspect | 1.12 | 1.11 | Vendor backport/source not obtained |

Do not relabel the upstream control files to hide these differences. None of the
profiles is claimed fully binary compatible with the vendor build. The same rule
applies to every yc-clone major and to previously produced packages.

## Build inputs

PostgreSQL archives are verified against the official SHA256 files:

| Profile | Source SHA256 |
| --- | --- |
| yc-15.18-icu70 | 11df0df97fe3ea4ba9a791faaf39cee1d2fe571e78885b5b55d8517d27c323b4 |
| yc-15.19-icu70 | e1a64a87a46b825b88c082e4518161a47aab53c45694964f8ba1df28f7859f89 |

Every selected external source archive is pinned by SHA256. The TimescaleDB
2.16.1 recipe selects APACHE_ONLY to match the managed catalog's Apache 2 edition.
The pgRouting GCC14 pointer fix and Timescale archive metadata patch are rebased
to these exact sources. pglogical's test configuration handles the
output_plugin_libraries GUC introduced in PostgreSQL 15.19, leaving it out on 15.18.
Core/contrib/extension tests stay enabled by default; test-full adds the long core
suites. Passing these tests qualifies the build itself, not vendor physical compatibility.
The Debian 13 package build backports the GIDX accessors from
[PostGIS 3.3.3](https://github.com/postgis/postgis/blob/3.3.3/libpgcommon/gserialized_gist.h)
to the pinned 3.3.2 source. This removes the oversized float-array cast reported
by GCC14 without changing GIDX structures, stored index layouts or the SQL
extension version. Installed-package checks compare GiST, SP-GiST and BRIN
index scans against sequential scans for 2D, 3D and 4D geometries, and verify
that each tested query actually uses its index. Compiler diagnostic checks
remain enabled. Physical WAL replay qualification has not been performed.

## Create a project

```bash
pg-maker init yc15 --profile yc-15.19-icu70
pg-maker validate yc15/build.yaml
# Alternative maintenance version:
pg-maker init yc15-18 --profile yc-15.18-icu70
```

Use the ordinary [build workflow](yc-clone-packages.md) with these profile names.
Expected names are `postgresql-15-yc-clone_15.19-1.deb13_amd64.deb`,
`postgresql-client-15-yc-clone_15.19-1.deb13_amd64.deb` and
`libpq5-yc-clone_15.19-1.deb13_amd64.deb`; replace 15.19 with 15.18 for that profile.
Binaries use `/opt/postgresql-yc-clone/15/bin`, PGDATA defaults to
`/var/lib/postgresql-yc-clone/15/data`, service `postgresql-yc-clone-15`.

## Available extensions outside standard contrib

The machine-readable catalog is `catalogs/yc-15.19-extensions.yaml` in every initialized project.
SQL versions do not uniquely identify vendor source commits or binary layouts.

| Extension | Managed SQL version | Candidate status | Source / reason |
| --- | --- | --- | --- |
| `address_standardizer` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `address_standardizer_data_us` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `anon` | 1.3.2.1 | blocked | The exact managed variant 1.3.2.1 and vendor patches have not been obtained; upstream 1.3.2 is not substituted. |
| `clickhouse_fdw` | 1.4 | selected-unverified | [pinned public source](https://codeload.github.com/adjust/clickhouse_fdw/tar.gz/a2cd63da0fbdd10be8e2948494e6cdc8bfc88016) |
| `hypopg` | 1.3.1 | selected-unverified | [pinned public source](https://codeload.github.com/HypoPG/hypopg/tar.gz/57d711bc4e37164c8edac81580a5f477e2a33d86) |
| `jsquery` | 1.1 | selected-unverified | [pinned public source](https://codeload.github.com/postgrespro/jsquery/tar.gz/40b1db58b3f3f4b315a6f606ba88752c0f8b6aaf) |
| `logerrors` | 2.1 | selected-unverified | [pinned public source](https://codeload.github.com/munakoiso/logerrors/tar.gz/9ea8b0134ecf4217cc7e7e7abff736db779a9713) |
| `lwaldump` | 1.0 | selected-unverified | [pinned public source](https://codeload.github.com/munakoiso/lwaldump/tar.gz/91d7a88e35fc75b5e04b68cc34d2b9a56ca7b34e) |
| `oracle_fdw` | 1.2 | blocked | Requires a separately provisioned Oracle Instant Client SDK/runtime; source alone is insufficient. |
| `orafce` | 4.1 | selected-unverified | [pinned public source](https://codeload.github.com/orafce/orafce/tar.gz/33cea9cd5ef6f839d0fcd9336a6767733a515c6e) |
| `pg_cron` | 1.5 | selected-unverified | [pinned public source](https://codeload.github.com/citusdata/pg_cron/tar.gz/0cb8118e8dfda30af58a65e14e2b7fe73ec6c931) |
| `pg_hint_plan` | 1.5 | selected-unverified | [pinned public source](https://codeload.github.com/ossc-db/pg_hint_plan/tar.gz/69747ab73062d763391ef6fc0ab17db49673ee74) |
| `pg_partman` | 4.7.4 | selected-unverified | [pinned public source](https://codeload.github.com/pgpartman/pg_partman/tar.gz/2330063ad29a65d169b1947ac35705a237774fb6) |
| `pg_qualstats` | 2.0.4 | selected-unverified | [pinned public source](https://codeload.github.com/powa-team/pg_qualstats/tar.gz/effe43b2c9f4c85822a98a09eac3681341a8fd86) |
| `pg_repack` | 1.5.3 | selected-unverified | [pinned public source](https://codeload.github.com/reorg/pg_repack/tar.gz/6902ab313aeb49227867087dca99cf107f7be23b) |
| `pg_stat_kcache` | 2.2.1 | selected-unverified | [pinned public source](https://codeload.github.com/powa-team/pg_stat_kcache/tar.gz/08a2f0f05ec42d08bda21805e88cc098c729b700) |
| `pg_stat_query_plans` | 1.0 | selected-unverified | [pinned public source](https://codeload.github.com/postgredients/pg_stat_query_plans/tar.gz/f296839e3fd772a21b01c487e66d31e1e7b45ae0) |
| `pg_tm_aux` | 1.1.1 | selected-unverified | [pinned public source](https://codeload.github.com/x4m/pg_tm_aux/tar.gz/857b9173069068741a52e8343bc532bd094fa2b9) |
| `pg_uuidv7` | 1.5 | selected-unverified | [pinned public source](https://codeload.github.com/fboulnois/pg_uuidv7/tar.gz/0396caaa8269a899826a9195f72c42a845d656a7) |
| `pgaudit` | 1.7 | selected-unverified | [pinned public source](https://codeload.github.com/pgaudit/pgaudit/tar.gz/8349710fbbe38ca9518ebe753a528b94f5ab2c19) |
| `pglogical` | 2.4.4 | selected-unverified | [pinned public source](https://codeload.github.com/2ndQuadrant/pglogical/tar.gz/bff71f2c6b0bd9748ecee15dcc93201cd1a145a0) |
| `pglogical_origin` | 1.0.0 | selected-unverified | [pinned public source](https://codeload.github.com/2ndQuadrant/pglogical/tar.gz/bff71f2c6b0bd9748ecee15dcc93201cd1a145a0) |
| `pgrouting` | 3.4.1 | selected-unverified | [pinned public source](https://codeload.github.com/pgRouting/pgrouting/tar.gz/441037f0c5b2b6fa9d1ff355c845fa15247803dd) |
| `pgvector` | 0.8.2 | blocked | Managed alias has no verified matching upstream control/SQL package; vector 0.8.2 is selected separately. |
| `plv8` | 3.2.3 | disabled | Explicit user instruction: keep PLV8 commented out. |
| `postgis` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `postgis_raster` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `postgis_sfcgal` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `postgis_tiger_geocoder` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `postgis_topology` | 3.3.2 | selected-unverified | [pinned public source](https://download.osgeo.org/postgis/source/postgis-3.3.2.tar.gz) |
| `rum` | 1.3 | selected-unverified | [pinned public source](https://codeload.github.com/postgrespro/rum/tar.gz/6a065fd8dfb280680304991aa30d7f72787fdb04) |
| `smlar` | 1.0 | selected-unverified | [pinned public source](https://codeload.github.com/jirutka/smlar/tar.gz/6259352edd179544c6f0e7c81bb68dcb81317f8c) |
| `spqrguard` | 2.4.2 | selected-unverified | [pinned public source](https://codeload.github.com/pg-sharding/spqrguard/tar.gz/207d46c91141c0e67bcf09432fbd9c760bd6b57f) |
| `spqrhash` | 1.2 | selected-unverified | [pinned public source](https://codeload.github.com/pg-sharding/spqrhash/tar.gz/3f4d326c88a69a4330c8399b4d3c833f25a6cf36) |
| `timescaledb` | 2.16.1 | selected-unverified | [pinned public source](https://codeload.github.com/timescale/timescaledb/tar.gz/7c78574c4d42ec7ceb840bd9d55652182221f251) |
| `vector` | 0.8.2 | selected-unverified | [pinned public source](https://codeload.github.com/pgvector/pgvector/tar.gz/cab9da72c04353f143bb06b42ab70a403daac64a) |
