# Standalone extension qualification — 2026-11-10

This record covers the unreleased standalone PGXS implementation, using native
amd64 containers and packaged PostgreSQL 18.6 SDKs. PostgreSQL itself was not
recompiled. See the [interface and commands](../interfaces/extensions.md).

## Initial native package matrix

The following matrix and timings describe the initial implementation before the
review fixes below. Its `+extensions1` migration packages are historical evidence;
they do not qualify the corrected migration identity or resume behavior.

| OS | PostgreSQL provider / native server version | Extension outputs |
| --- | --- | --- |
| Debian 13 | PGDG `postgresql-18`, `18.6-1.pgdg13+2` | Three DEBs |
| Debian 13 | `yc-18.6-icu70`, `postgresql-18-yc-clone`, `18.6-1.deb13+extensions1` | Three extension DEBs and three revised database DEBs |
| Rocky Linux 10 | PGDG `postgresql18-server`, `18.6-4PGDG.rhel10.2` | Three RPMs |

| Extension | Source/package upstream version | SQL version | Native source tests per target |
| --- | --- | --- | ---: |
| pg_wait_sampling | 1.1.11 | 1.1 | 2 |
| pg_stat_kcache | 2.3.1 | 2.3.1 | 1 |
| pg_repack | 1.5.3 | 1.5.3 | 11 |

All 14 source tests passed on each target. Each target also passed installation
checks in a new container with the matching OS, no PostgreSQL preinstalled, no
build sources or object files, and network disabled. The checks covered:

- Native rejection of each extension package without its server dependency.
- Installation from a pinned local runtime repository; payload hashes, modes and
  root ownership checked against the package inventory.
- `CREATE EXTENSION`, SQL versions and dependency resolution.
- kcache activity records, sampled `PgSleep` events, and a pg_repack run preserving
  a checksum of table contents.
- Database restart and package removal/reinstallation, preserving sentinel data.
- Verified artifact catalog and export with source/package inputs; successful
  runs removed their object trees and test PGDATA.

YC qualification used the explicit `--split-bundled` migration because the original
server/client packages already owned kcache/repack files. It produced revised
copies, preserving every unrelated payload file and full standard contrib. The
original database DEBs were not changed. The new YC extension DEBs depend on the
exact revised server version and must be installed with that migration set.

## Measured build time

These are the final three-extension runs with already prepared SDK images and
source inputs, running concurrently on one native amd64 builder. Each run compiled
all three extensions without object reuse. Network downloads and initial SDK
preparation are excluded. Total time includes container startup, SDK verification,
repository transfer and cleanup in addition to the stages below.

| Target | Compile | Package | Source tests | Clean-container tests | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Debian 13 / community | 3.4 s | 2.7 s | 20.8 s | 49.0 s | 84.0 s |
| Debian 13 / YC | 3.4 s | 2.2 s | 16.3 s | 77.1 s | 107.2 s |
| Rocky Linux 10 / community | 7.6 s | 2.5 s | 19.5 s | 40.6 s | 78.6 s |

## Diagnostics and limits

The native source and packaging logs were classified and inspected. pg_repack's
three missing-variable-declaration warnings concern its `PROGRAM_*` metadata
variables. RPM's generic FHS checker warns about PGDG's `/usr/pgsql-18/lib`; the
builder first validates every ELF runtime search path against the exact SDK
directories, rejecting temporary, relative, empty or unrelated paths. Expected
negative dependency/SQL checks are retained in the logs. Removing a temporary YC
package can warn that the shared `/opt` directory is not empty. These were not
treated as test failures or hidden from the saved evidence.

The implementation initially supports PGXS recipes. Other adapters fail explicitly.
This matrix does not qualify ARM64, Rocky 9, every PostgreSQL major, arbitrary
vendor ABI compatibility, or physical replication. Previous-extension-version SQL
upgrade testing is reported as `not-configured`; reinstall is tested separately.
Database core tests are outside the scope of extension-only packaging.

The Python utility passed 469 unit/contract tests. Additional cases cover immutable
inputs and retained-source tampering, SQL dependency cycles, package identity and
metadata validation, RPM payload comparison, unsafe runtime search paths, export
tampering and preservation of unrelated YC/contrib files during migration.
Ruff, generated schemas, wheel/sdist content verification, strict Twine checks and
installation of the wheel in an isolated environment also passed.

## Review fixes

The follow-up implementation addresses all eight findings:

1. Synchronous `extension prepare/build` waits on the operation ID returned by the
   service, rather than a nonexistent `operation_directory` field.
2. Prepared SDK identities include the selected bundled modules. A repack-only
   migration cannot reuse an SDK with kcache removed.
3. Migration native versions include a digest of the selected modules and original
   input DEBs (`+extensions2.<digest>`). Exact-SDK extension releases also include
   `+sdk.<digest>`, so different dependency sets do not share a package filename.
4. Failed/cancelled extension operations produce matching machine status and a
   nonzero CLI exit code, including detached retries of completed operations.
5. Resume removes old PostgreSQL test tablespace version directories within the
   owned workspace, preserving source/object files and unrelated contents.
6. CLI and API preparation/build requests honor `request_id`; unchanged retries
   reuse an operation, while changed inputs with the same ID are rejected.
7. Declared runtime dependencies are installed in the SDK as well as retained in
   the offline repository for clean-container installation tests.
8. Container commands stream stdout/stderr immediately, retaining partial output
   when a timeout or cancellation interrupts the command.

These migration variants are alternative complete sets, not an ordered release
sequence. Install extensions with their exact matching database set; choose the
combined selection when both bundled modules must become independent packages.
Increment `package_revision` for subsequent extension releases. Regenerate locks
after changing the builder implementation.

The corrected utility passed **481 unit/contract tests**, Ruff, wheel/sdist content
checks and strict Twine validation. An isolated wheel installation also exercised
the real synchronous CLI failure path and repeated request IDs.

### Corrected native qualification

All four corrected runs completed successfully on the native amd64 builder:

| Target | Source tests | Fresh-container package tests | Result |
| --- | ---: | --- | --- |
| Debian 13 / community, three extensions | 14 | Passed | Cancelled after source tests; resumed with all three build receipts reused |
| Debian 13 / YC, three extensions | 14 | Passed | Selected kcache/repack files migrated; unrelated payload preserved |
| Rocky Linux 10 / community, three extensions | 14 | Passed | RPM dependencies, payload and runtime behavior verified |
| Debian 13 / YC, pg_repack only | 11 | Passed | kcache remains bundled; distinct SDK and package identities verified |

The complete YC selection uses database version
`18.6-1.deb13+extensions2.a3817d739abb5a6ab650` and extension release suffix
`1.deb13+sdk.77b391632b2a52cc1be5`. The repack-only selection uses database version
`18.6-1.deb13+extensions2.d577949a3665a69d64f9` and extension release suffix
`1.deb13+sdk.0b092a567a932c682380`. Package names remain unchanged. Community
server versions remain those in the initial matrix above.

Each run verified missing-server dependency rejection, installed inventory,
extension SQL versions and functional behavior, restart, and removal/reinstallation
without losing sentinel data. Artifact catalog and export verification passed.
Successful runs removed their build scratch and test clusters. Repeating the
successful YC CLI command with the same request ID reused its original operation.
The cancelled Debian CLI returned exit code 7 and machine status `cancelled`;
its resumed CLI returned exit code 0 and status `succeeded`.

The corrected runs were sequential, using prepared SDKs and offline containers.
The Debian row measures only the successful resumed attempt; its source and
package tests ran again, while compilation reused completed components.

| Target | Compile / reuse | Package | Source tests | Clean-container tests | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| Debian 13 / community, resumed | 0.01 s | 1.9 s | 9.0 s | 26.3 s | 40.8 s |
| Debian 13 / YC | 2.5 s | 2.0 s | 8.6 s | 43.8 s | 63.8 s |
| Rocky Linux 10 / community | 5.7 s | 2.0 s | 8.6 s | 22.3 s | 44.7 s |
| Debian 13 / YC, pg_repack only | 1.3 s | 0.7 s | 8.1 s | 42.5 s | 58.9 s |

Initial parallel SDK preparation encountered Docker API timeouts. Failed
preparations were resumed sequentially and completed successfully before these
builds. No source or package tests were excluded to obtain the results above.
