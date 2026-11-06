# pg-maker project

`build.yaml` selects the PostgreSQL version, feature, platform and component set.
Edit branding in `config/vendor.yaml`, and paths, build resources and service settings
in `config/defaults.yaml`. The build UID/GID must match the non-root workspace owner.

Without `--profile`, init selects PostgreSQL 18.1 / Debian 13 / x86_64, `my-feature`,
and pg_stat_statements, pg_stat_kcache and pg_wait_sampling. YC profiles select
PostgreSQL 16.15, 17.11 or 18.6 with private ICU 70.1 and their extension catalogs.
Their binaries use `/opt/postgresql-yc-clone/<major>/bin`, PGDATA uses
`/var/lib/postgresql-yc-clone/<major>/data`, and the unit is
`postgresql-yc-clone-<major>.service`. Debug adds `-debug` to the major.
YC server packages include headers and PGXS; generic defaults omit the SDK.

```bash
pg-maker validate build.yaml
pg-maker config show build.yaml --resolved
pg-maker env prepare build.yaml --input-store inputs --out environments.json --operations operations
pg-maker resolve build.yaml --environments environments.json --out lock.json --input-store inputs --operations operations
pg-maker plan lock.json --out plan.json --goal package
pg-maker build lock.json --plan plan.json --operations operations --operation-id build --detach
pg-maker wait operations/build
pg-maker export operations/build --out delivery
```

`env prepare` installs tools and dependencies in Docker and records reusable build/test
image IDs. `resolve` freezes source hashes and patches. `build` starts the prepared
image automatically, mounts the operation workspace and compiles offline. DEBs appear
on the host under `operations/build/workspace/output/<variant>.candidate/packages/`;
export copies them to `delivery/output/<variant>.candidate/packages/` with reports.
Use a new operation ID for another build.

Database and component tests run by default; package builds also check installation
in fresh containers. To omit them explicitly, create the plan with `--skip-db-tests`.
The build inherits that saved setting and reports `unverified`, `db_tests=skipped`
and `publication_eligible=false`. Enabled documentation retains rendering and installed-doc
checks without starting PostgreSQL. `test-full` adds expensive core suites. The service
emulator does not establish native systemd qualification.

Configure documentation before environment preparation and resolve. With documentation
enabled, HTML/PDF go into a fourth DEB; manpages follow their command packages.
Russian output requires a complete external translation bundle.

Installation creates an empty PGDATA; it does not run initdb/start or activate
extensions. Initialize or restore the intended cluster explicitly, configure preload
libraries and create extensions following their manuals. A path change does not move
an existing database automatically.

Long operations support `--detach`; use status/events/wait/cancel with the operation
directory. Logs are in `attempts/N/logs`, `attempts/N/progress.log` and `worker.log`.
Resume retries the same locked inputs. Changed code/configuration needs a new resolve
and build. Repeated init retains identical files and reports conflicts.

Source caches, prepared images and matching component outputs accelerate later builds.
Shared cache defaults to `operations/.cache`; `--no-cache` disables shared reuse but
preserves private checkpoints. Export before explicit cleanup. Monitor disk usage;
global Docker pruning is never automatic.

Pip installs manuals under `<installation-prefix>/share/doc/pg-maker`, including
`docs/yc-clone-packages.md` for building, uploading and installing packages.
