# pg_maker project

The initial target is PostgreSQL 18.1 / Debian 13 / x86_64 with feature my-feature.
Edit vendor Test, paths, build modes and resources in config/. Defaults enable the
common socket patch. Configuration and file logs live in PGDATA; headers, PGXS and
ecpg are not shipped.

The feature selects pg_stat_statements, pg_stat_kcache 2.3.2 and pg_wait_sampling 1.1.11
(SQL version 1.1). Editable component recipes pin external sources by commit SHA;
pg_stat_statements uses PostgreSQL contrib. Set components: [] for no extra extensions.
Do not remove pg_stat_statements while pg_stat_kcache requires it.

```bash
pg-maker validate build.yaml
pg-maker config show build.yaml --resolved
pg-maker package names build.yaml
pg-maker env prepare build.yaml --out environments.json
pg-maker resolve build.yaml --environments environments.json --out lock.json --input-store inputs
pg-maker plan lock.json --out plan.json --goal build
pg-maker build lock.json --plan plan.json --detach
```

Environment preparation installs dependencies into separate Docker images. Resolve
freezes sources/changes; offline resolve reuses stored inputs without networking.
Plan verifies hashes and describes work. Database/component tests, acceptance and
baseline are disabled by default for fast development; results remain unverified.
Use build --run-db-tests for full testing, or create a matching saved plan first.
The default is config/defaults.yaml spec.validation.run_db_tests. Utility tests are
unaffected. Build --goal package produces three DEBs and manifests; verified requests
long source/installed suites. The systemctl emulator does not qualify native systemd.
The build user must match the non-root host workspace owner.

Enable documentation in config/documentation.yaml before environment preparation and
resolve. Docs build produces selected HTML/man/PDF; prepare-translations exports bundles
for an external translator. Register completed bundles under spec.translations and
resolve again. Rendering is offline. With docs, a fourth docs package is added and
server only Suggests it. Selected external-component guides join navigation/search/PDF;
Russian builds require corresponding translation bundles.

Package installation does not activate extensions in existing clusters. Configure
shared_preload_libraries in order pg_stat_statements,pg_stat_kcache,pg_wait_sampling,
restart and create extensions in the needed databases, following their manuals.

Long operations support --detach and --request-id. Status/events/wait/cancel accept
operations/OPERATION_ID. Do not edit active input/state/journal files. Logs live under
attempts/N/logs, progress under attempts/N/progress.log and worker output in worker.log.
Repeated init retains identical files and reports conflicts before any remaining writes.

Resume build with the original plan hash; preparation requires its input hash. Changed
code belongs in a new resolve/build or a dev session. Dev open --from-run creates an
owned Git workspace for intermediate commits, patch replay and selected component builds.

Export preserves packages/reports/evidence; --include-inputs adds source trees/blobs,
not Docker images. Shared cache defaults to operations/.cache. No-cache disables shared
reuse but preserves private checkpoints. Preview cache prune before applying it.
Monitor df/du/docker system df. Export needed results before explicit scratch cleanup;
cleaned runs cannot resume. Global Docker prune is never automatic.
