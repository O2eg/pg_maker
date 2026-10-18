# Stages 5–6 validation — 2026-10-17

Environment: Linux, Python 3.12, local Docker, PostgreSQL 18.1 / Debian 13 / x86_64.
Long database tests were disabled by user instruction. Packages remained unverified,
db_tests=skipped and publication_eligible=false.

## Executed checks

- Local suite: 197 passed, one explicitly disabled long PostgreSQL test skipped,
  213 seconds. Python 3.10.21 installed-wheel unit/contracts: 174 passed in 27 seconds,
  plus CLI/init smoke. Final schema compatibility changes received 15 focused checks.
- Real PostgreSQL compilation, core/world-bin install, InstallTree, ELF analysis,
  shlibdeps and three DEBs from commit 4b324845ba5d24682b9b3708a769f00d160afbd7.
  Inventory contained 1,535 files/links, 1,654 entries including directories and
  1,422 reasoned exclusions. DEB bytes/modes/ownership matched inventory.
- Offline installation of libpq-only, client set and full server set in fresh containers
  without build trees. Hashes/permissions, loader, client versions, no implicit initdb,
  empty-PGDATA startup rejection and data-preserving removal/purge passed. Checking
  initdb with ldd did not execute initdb.
- Small Make-fixture worker SIGKILL, exact-resource cleanup, new attempt, verified
  compile checkpoint reuse, old log retention and wrong-hash rejection.
- Partial matrix recovery, preparation resume reusing a completed build image,
  shared cache hits/corruption recovery, private mutable roots and stale-prune rejection.
- Snapshot publication only after referenced file/directory fsync, including retry
  after interruption between copying and fsync. One 10,162-file capture took 5,868 ms;
  verification took 601 ms. These are local observations, not performance guarantees.
- Export relocation with relative references/hashes and reused command logs, including
  full input export. Three DEBs/manifests were rechecked; preparation/resolve/build
  succeeded with no remaining resource references.
- Baseline failure blocked candidate publication eligibility. Parameterized controls
  covered vendor/version/architecture changes without claiming ARM64/RPM qualification.
- Ruff, schema regeneration, wheel/sdist and Twine passed.

Historical delivery layout under /tmp/pg-maker-stage56-live-20261017:

```text
environments-v2.json, lock.json, cache-benchmark.json
operations/package-postgresql/
delivery/
  output/my-feature.debian13-x64.candidate/
    packages/*.deb
    package-specs/*.json
    install-tree.json, excluded-files.json, runtime-dependencies.json
    package-tests.json, package-tests/{libpq,client,server}/
    artifact-manifest.json, verification.json, INSTALL.txt
    report.json, report.html
  inputs/
  export-manifest.json
```

| Role | Package, version 18.1.0-1.deb13 | Bytes |
| --- | --- | ---: |
| server | test-my-feature-server-18 | 13,784,604 |
| client | test-my-feature-client-18 | 749,972 |
| libpq | test-my-feature-libpq5-18 | 146,244 |

Database check/check-world, installed SQL/backup/crash/checksum/acceptance and real
predecessor database upgrades were not run. Their orchestration does not make this
report package-verified. Native systemd/cgroups were not checked; the harness reports
emulation. Remote Actions was not run: workflows/common CLI script, short fixtures
and manual PostgreSQL jobs were prepared with database tests off by default.
At this stage, RPM/documentation/publication were future work; later reports describe
documentation implementation.

Implementation changes require a new resolve for execution. Exported DEB bytes/hashes
remain independently verifiable after source code changes.
