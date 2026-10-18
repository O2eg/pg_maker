# Machine interface

pg_maker implements `pg_play/component/v1` without importing pg_play. `--machine`
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
plans and exact plan hashes. Build/preparation/runtime operations have explicit
resume; arbitrary container commands and development debug commands do not.
Partial status remains explicit, with exit code 5.

Build reports include `db_tests: skipped|passed|failed`. Quick builds are unverified
and ineligible for publication. Test mode is part of plan identity. Success without
tests means the selected build steps completed, not database qualification.

pg_play registers maker as optional and exposes typed CLI/MCP lifecycle, artifact
and development operations. Artifact queries verify files and return package,
manifest, verification and report references; envelopes carry those references.
Build/status expose known output references as well. Capability secret-policy fields
are public metadata and must not be replaced with redaction masks. Migration to
advisories/v2 is a separate compatibility decision.
