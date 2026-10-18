# Runtime image handoff

`integrations.runtime.plan_runtime(artifact_directory, profile) -> RuntimePlan`
verifies artifact/evidence manifests, exactly three runtime DEBs from one variant,
its prefix and required diagnostic extensions. The initial adapter is Debian 13 /
x86_64. Profiles supply a pinned base image, files with hashes, entrypoint filename,
extension/library lists and disk/memory limits. These choices belong to pg_play's
profile, not hardcoded pg_maker names. pg_play supplies pg_stand files; maker has no
runtime dependency on either sibling package.

`pg-maker runtime plan DIR --profile FILE --out PLAN` creates
`pg_maker/runtime-plan/v1`. Its hash covers inputs and qualification.
`runtime build PLAN --plan-hash HASH --out DESCRIPTOR --detach` runs a durable
runtime_image operation through existing worker/ownership/event/cancellation code.
Resume requires the original runtime plan hash. Changed packages, evidence, scripts
or dependency DEBs are rejected. Planning requires a pinned runtime repository and
exact file inventory. Each SQL extension needs library, control and installation
SQL; upgrade SQL alone is insufficient. Plain libraries such as auto_explain need
no SQL files.

The worker installs local packages without networking, verifies OS/architecture,
pg_config, programs and shared libraries, then commits an image ID. Its owned
container is removed in finally. The `pg_maker/runtime-image/v1` result records image,
runtime/build plan and artifact manifest hashes, prefix, PostgreSQL version,
qualification, extensions, user, `database_started=false` and document hash.
Qualification is never promoted by image creation.

No database is initialized. Subsequent pg_stand entrypoint startup and SQL/workload
acceptance are separate actions. Disk checks precede preparation, DEB copying and
image commit; these are thresholds, not quotas. The committed image is retained as
an output rather than automatically removed.
