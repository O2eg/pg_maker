# pg_maker / pg_play integration validation — 2026-10-17

Subsequent packaging fixes are recorded in [review fixes](review-fixes.md). The local
environment was Linux x86_64, Python 3.12, Docker, Debian 13 and PostgreSQL 18.1.
Database build suites, initdb and server startup were not executed.

Implemented integration included an optional pg_play component, typed CLI/MCP,
package-to-runtime-image conversion, maker experiment phase, provenance retention,
private agent source workspaces and ChangeSet export. Contracts:
[runtime](../interfaces/runtime.md), [changes](../interfaces/changes.md) and
[cleanup](../interfaces/cleanup.md). The companion user workflow lives in
pg_play/docs/pg_maker-integration.md.

## Executed checks

- 199 maker unit/contract and 124 pg_play tests: reviewed-plan immutability, repeated
  operation IDs, real machine envelopes, experiment ordering, immutable stand image
  binding, cancellation after coordinator loss, resume and unverified qualification.
- Twelve small maker Docker tests and one cross-process pg_play orchestration test:
  workers, cancellation, events, artifact/export and owned-container/anonymous-volume
  cleanup, without starting PostgreSQL.
- All five example diagnostics compiled against the private PostgreSQL 18.1 pg_config:
  pg_stat_statements, pg_buffercache, auto_explain, pg_wait_sampling and pg_stat_kcache.
  External sources were commit-pinned; installcheck was not run.
- Public pg_play methods built a runtime image offline using earlier real DEBs plus
  a fixture server DEB containing those compiled extensions. This tested the runtime
  adapter, not a fresh complete distribution build.
- The image contained PostgreSQL 18.1/pg_config under /opt/test/my-feature/18, libraries,
  control files and resolved ldd dependencies, without initialized PGDATA. Qualification
  remained unverified.
- Unavailable Docker returned precondition_failed. After environment recovery, resume
  with the same runtime plan hash succeeded on attempt 2, progress 1/1 and no resources.
  Export hashes verified.
- Private workspaces preserved CAS. Exported patches reapplied byte-for-byte, including
  additions/deletions/no-final-newline; binary, mode and symlink changes were rejected.
- Cleanup required a verified external export, terminal operation and unchanged plan;
  symlink substitution was rejected and outputs retained.
- Both projects passed Ruff/build/Twine. Wheels imported outside checkout, runtime
  assets were available, and contracts/example recipes were included in sdists.

Compact evidence was saved under /tmp/pg-maker-integration-delivery-20261017, including
operation export, test logs, runtime/resume evidence and distributions. Test-owned
scratch, fixture DEBs and images were removed after evidence retention; shared images
and earlier-stage data were not globally cleaned.

Full stand experiments with PostgreSQL startup, SQL, PgBouncer, replication and workload
have not received live acceptance in this stage. Runtime/export never promote package
qualification. Database tests stay explicit; RPM/ARM64, repository publishing and
runtime image registry/archive transport remain separate work.
