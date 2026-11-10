# Machine interface

pg-maker implements `pg_play/component/v1` without importing pg_play. `--machine`
reserves stdout for one JSON envelope. Request IDs propagate through responses and
idempotent startup. Both flags work before or after the command.
`--component-capabilities` returns capabilities directly, or inside the standard
machine envelope. It does not require Docker or claim an environment check.

Envelope fields: contract_version, component=pg_maker, component_version, command,
request_id, status, result, artifacts, warnings and error. Version-1 warnings are
strings. Internal interrupted status maps to failed externally and remains in the
result. Status/events/cancel report successful reading/requesting independently of
the operation's outcome.

Capabilities use `pg_play/capabilities/v1` and list actual commands, mutation flags,
machine output, plan-hash acceptance, exit codes, secret policy, document schemas,
artifact roles, operation kinds/resume and environment_checked. Build accepts saved
plans and exact plan hashes. Database builds and runtime operations resume using
their plan hashes; preparation, standalone extensions and publication use input
hashes. Arbitrary container commands and development debug commands do not resume.
Partial status remains explicit, with exit code 5.

Build reports include `db_tests: skipped|passed|failed`. Skipping source suites keeps
the build unverified and its legacy `publication_eligible` flag false. Built-in
publication still accepts explicitly configured package-test-only builds with sealed
passing installed-database evidence. Completely untested packages are rejected.
Test mode is part of plan identity; see the [test-mode matrix](build.md#test-modes-and-publication).

pg_play registers maker as optional and exposes typed CLI/MCP lifecycle, artifact
and development operations. Artifact queries verify files and return package,
manifest, verification and report references; envelopes carry those references.
Build/status expose known output references as well. Capability secret-policy fields
are public metadata and must not be replaced with redaction masks. Migration to
advisories/v2 is a separate compatibility decision.
