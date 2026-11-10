# pg-maker

Build PostgreSQL, extensions, DEB packages and documentation in isolated Docker
containers. pg-maker manages source versions, prepared build images, build plans,
tests, recovery and reusable compilation results. Git development sessions support
editing core/extension code and maintaining patch series.

The bundled platform is **Debian 13 / amd64** with Autotools and release/debug builds.
The PostgreSQL catalog covers **14–19**; database-test evidence adapters support
**15, 16, 17 and 18**. Catalog support does not establish qualification of every build.
ARM64 database builds require additional platform/component adaptations. Standalone
PGXS extensions support DEB and RPM packaging against an installed PostgreSQL SDK;
see [extension packages](docs/interfaces/extensions.md). Database RPM distributions
and remote execution are not implemented. Tested native packages can be published to
Nexus APT/YUM/Raw or filesystem receivers; see [CI/CD publication](docs/interfaces/publication.md).
See [implementation limits](docs/roadmap.md).

## How it works

Database distributions and standalone extensions follow the same conceptual lifecycle:

```text
  Database profile / build.yaml          Extension project / extensions.yaml
  PostgreSQL + libraries + contrib       Selected modules + target PostgreSQL SDK
  + additional extensions                            |
                 |                                   |
                 +-----------------+-----------------+
                                   v
                 +-----------------------------------+
                 | Prepare environments and inputs   |
                 | Sources, patches, tools, images   |
                 | Freeze hashes in a lock; plan     |
                 +-----------------+-----------------+
                                   v
                 +-----------------------------------+
                 | Build in isolated Docker          |
                 | Compile offline; stage components |
                 | Run source tests by default       |
                 +-----------------+-----------------+
                                   v
                 +-----------------------------------+
                 | Create native packages            |
                 | Database: DEB                     |
                 | Standalone extensions: DEB / RPM  |
                 +-----------------+-----------------+
                                   v
                 +-----------------------------------+
                 | Test packages in fresh containers |
                 | Install, start/use, remove        |
                 | Check dependencies and contents   |
                 +-----------------+-----------------+
                                   v
                 +-----------------------------------+
                 | Host outputs / portable export    |
                 | Packages + hashes + test evidence |
                 +-----------------+-----------------+
                                   | optional, tested artifacts only
                                   v
                 +-----------------------------------+
                 | Publish to configured receivers   |
                 | Nexus APT/YUM/Raw or directory    |
                 | Verify bytes and native indexes   |
                 +-----------------------------------+
```

The CLI or Python API controls durable operations on the host; containers perform
compilation and installation checks. Failed stages stop the affected build path.
Saved state supports status, logs and resume; matching prepared images, inputs and
component results can be reused. Changed inputs require a new lock and plan.
Expensive core suites are opt-in through `test-full`. Publication verifies APT/YUM
indexes for native repositories and file hashes for every receiver.

## Install

Requires Linux and Python 3.10+. Builds require access to a local Linux Docker daemon;
Git sources and development sessions also require Git. Run the controller as the
non-root owner of the project workspace. Reading help/capabilities does not need Docker.

Install a published version from PyPI:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install pg-maker
pg-maker --version
pg-maker --help
```

Or install the current source, including unreleased changes:

```bash
git clone https://github.com/O2eg/pg_maker.git
cd pg_maker
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pg-maker --version
pg-maker --component-capabilities
pg-maker doctor --machine
```

A wheel can be installed with `python -m pip install /path/to/pg_maker-VERSION-py3-none-any.whl`.
The product and CLI name is `pg-maker`; `pg_maker` is the repository/Python module name
and the normalized wheel filename. Pip supplies the TOML reader needed by Python 3.10.

## Choose and configure a profile

`init` writes editable configuration into a new project directory. Choose one profile:

| `init` option | PostgreSQL | Contents |
| --- | --- | --- |
| No `--profile` | 18.1 | `my-feature`, system ICU, pg_stat_statements, pg_stat_kcache 2.3.2 and pg_wait_sampling 1.1.11 |
| `--profile yc-15.18-icu70` | 15.18 | Same ICU and extension recipes as `yc-15.19-icu70` |
| `--profile yc-15.19-icu70` | 15.19 | Private ICU 70.1 and the [PG15 extension catalog](docs/managed-15-icu70.md) |
| `--profile yc-16.14-icu70` | 16.14 | Same ICU and extension recipes as `yc-16.15-icu70` |
| `--profile yc-16.15-icu70` | 16.15 | Private ICU 70.1 and the [PG16 extension catalog](docs/managed-16-icu70.md) |
| `--profile yc-17.10-icu70` | 17.10 | Same ICU and extension recipes as `yc-17.11-icu70` |
| `--profile yc-17.11-icu70` | 17.11 | Private ICU 70.1 and the [PG17 extension catalog](docs/managed-17-icu70.md) |
| `--profile yc-18.4-icu70` | 18.4 | Same ICU and extension recipes as `yc-18.6-icu70` |
| `--profile yc-18.6-icu70` | 18.6 | Private ICU 70.1 and the [PG18 extension catalog](docs/managed-18-icu70.md) |

Every YC profile targets managed physical binary compatibility, which remains
**unverified**. See the [compatibility contract](docs/yc-clone-compatibility.md);
successful build/package tests do not certify vendor equivalence.

Within each YC major, maintenance-version profiles differ only in the PostgreSQL
version, source archive URL/SHA256 and manifest name. Component versions, patches,
build options, package revision, paths and default test policy stay the same.

All bundled profiles target amd64. PLV8 is disabled in all YC profiles. Their
catalogs record unavailable extensions and qualification limits; matching SQL extension
versions does not establish managed-vendor binary or physical-replication compatibility.
The [package walkthrough](docs/yc-clone-packages.md) also describes the ARM64 boundary.

The following example builds the PG18 YC profile. Use a separate directory for each
profile; repeated `init` accepts identical files but refuses to overwrite edited ones.

```bash
pg-maker init example --profile yc-18.6-icu70
```

Review configuration before preparing images or locking inputs:

| File | Settings |
| --- | --- |
| `example/build.yaml` | PostgreSQL source/version, selected features/targets, package revision and profile overrides |
| `example/config/vendor.yaml` | Product branding and vendor slug |
| `example/config/defaults.yaml` | Installation/data paths, service, build user, jobs, resource limits and default test policy |
| `example/config/features/*.yaml`, `example/components/` | Component selection, source pins, patches and build/test recipes |
| `example/config/documentation.yaml` | Documentation languages/formats, branding and translations |

Defaults use UID:GID `1000:1000`, four jobs, an 8 GiB memory limit and a 20 GiB free-disk
reserve. Match the build user to `id -u`/`id -g` and resources to the host. The reserve
is additional headroom, not an estimate of total storage; large extension builds need
space for sources, objects, checkpoints, caches and Docker images. Documentation is
disabled initially. `init --build-mode debug` selects debug flags and separate names.

```bash
pg-maker validate example/build.yaml
pg-maker config show example/build.yaml --resolved
pg-maker package names example/build.yaml
```

Existing projects keep their copied YAML when pg-maker is upgraded. Changes to source,
configuration, recipes or controller code require a new resolve and plan. Dependency
changes also require matching prepared environments. See [configuration](docs/interfaces/configuration.md).

## Build and export DEB packages

```bash
pg-maker env prepare example/build.yaml --input-store example/inputs --out example/environments.json --operations example/operations
pg-maker resolve example/build.yaml --environments example/environments.json --out example/lock.json --input-store example/inputs --operations example/operations
pg-maker plan example/lock.json --out example/plan.json --goal package
pg-maker build example/lock.json --plan example/plan.json --operations example/operations --operation-id build --detach
pg-maker status example/operations/build
pg-maker wait example/operations/build
pg-maker artifacts example/operations/build
pg-maker export example/operations/build --out example/delivery
```

`env prepare` downloads the base image, installs tools/dependencies in containers and
records reusable build/test image IDs. `resolve` downloads and freezes source commits,
submodules, archives, patches and checksums. Both need network access for missing inputs;
`resolve --offline` reuses a populated input store. An environment JSON file references
Docker images and does not contain them.

`build` starts the prepared container automatically, mounts the operation workspace and
compiles without networking. No manual `docker run` is needed. `--detach` starts a worker;
`wait` reports its terminal result. Use a new operation ID and export destination for a
new run. The [recovery contract](docs/interfaces/recovery.md) covers retries of failed runs.

For this profile, DEBs appear **on the host** under:

```text
example/operations/build/workspace/output/yc18-icu70.debian13-x64.candidate/packages/
example/delivery/output/yc18-icu70.debian13-x64.candidate/packages/  # exported copy
```

The three roles are server/extensions, client programs and libpq. Reports, manifests,
checksums and `INSTALL.txt` accompany the branch outputs. With documentation enabled,
HTML/PDF have a separate docs DEB; manpages follow their command packages.

All YC profiles use `/opt/postgresql-yc-clone/<major>/bin`,
`/var/lib/postgresql-yc-clone/<major>/data` and `postgresql-yc-clone-<major>.service`.
Debug appends `-debug` to the major. Installation creates an empty data directory;
it does not run initdb or start a cluster. Existing data is not relocated automatically.
Follow the [Nexus upload and Debian installation guide](docs/yc-clone-packages.md).

## Test modes and qualification

**Database tests run by default.** Normal builds run core regression, `check-world`
(contrib, isolation and TAP), selected component tests and acceptance checks. Package
builds add installation/lifecycle checks in fresh offline containers. Source tests
can grant `source-verified`; package qualification requires all installed-package gates.
Historical qualification records cover their exact artifacts and installation paths.

| Goal | Result |
| --- | --- |
| `source` | Prepared source trees; no compilation or database tests |
| `build` | Compiled and installed build tree, with source tests by default; no DEBs |
| `package` | DEBs and installed-package qualification by default |
| `verified` | Package goal that requires tests, even if the locked default is false |
| `test-full` | Standard source/component tests plus expensive core suites; no DEBs |
| `docs` | Selected documentation formats; no server compilation or DEBs |

To build packages explicitly without database tests, use a separate saved plan:

```bash
pg-maker plan example/lock.json --goal package --skip-db-tests --out example/quick-plan.json
pg-maker build example/lock.json --plan example/quick-plan.json --operations example/operations --operation-id quick --detach
pg-maker wait example/operations/quick
```

Build inherits the saved plan's test mode; conflicting flags are rejected. Skipped-test
results are `unverified`, `db_tests=skipped`, `publication_eligible=false`. Structural
build/package checks remain; enabled documentation still has rendering and installed-doc
checks. With the default `run_package_tests: false`, this example also skips package
tests. Set `spec.validation.run_package_tests: true` before preparation/resolve to
retain them when source tests are disabled. An external Nexus upload does not change qualification. Built-in publication requires
successful installed-package tests even when source tests were intentionally skipped.

`pg-maker test-full example/lock.json --operations example/operations` adds WAL consistency
and, for PG17/18, XID wraparound suites. To require these before packaging, set
`spec.validation.test_suites: [standard, long]` in `build.yaml` before resolve. Mandatory
extra IDs belong in `spec.validation.required_tests`; missing evidence fails qualification.
Majors 14/19 need explicit skipped-test builds until evidence adapters exist.
See [test policy and diagnostics](docs/interfaces/build.md) and [packaging](docs/interfaces/packaging.md).
The [test-mode matrix](docs/interfaces/build.md#test-modes-and-publication) distinguishes
standard builds, package-test-only rebuilds and completely untested packages.

## Build PostgreSQL documentation

```bash
pg-maker init docs-project --with-doc --doc-language en
pg-maker env prepare docs-project/build.yaml --input-store docs-project/inputs --out docs-project/environments.json --operations docs-project/operations
pg-maker resolve docs-project/build.yaml --environments docs-project/environments.json --out docs-project/lock.json --input-store docs-project/inputs --operations docs-project/operations
pg-maker docs build docs-project/lock.json --operations docs-project/operations
```

The enabled default formats are HTML/man. Add `pdf` to `spec.formats` in
`config/documentation.yaml` **before** environment preparation and resolve to build PDF.
The default HTML uses `sphinx_book_theme`; `html_backend: docbook` selects upstream
DocBook output. Use `branding: none` for upstream branding. Inputs may be official
archives, Git commits or snapshots of locally modified PostgreSQL sources.

Russian output needs a complete, current external translation bundle. The standalone
`sgml-writer` and `sgml-translate` commands use the same locked pipeline. The controller's
own Markdown manuals are English. See [documentation and translation](docs/interfaces/documentation.md).

## Diagnose, resume and reuse work

```bash
pg-maker status example/operations/build --machine
pg-maker events example/operations/build --after-sequence 0 --limit 20 --machine
pg-maker wait example/operations/build --timeout 120 --machine
# To stop an active operation:
pg-maker cancel example/operations/build
```

Progress and command logs live under the operation's `attempts/N/`; detached worker
output is in `worker.log`, and branch reports are under `workspace/output/`. `logs verify`
checks saved compiler/test logs against a diagnostic policy; see [diagnostics](docs/interfaces/build.md#diagnostic-policy).
`status` succeeds if it can read state, even when the operation failed. `wait` returns
the operation's exit code; a waiting timeout or closing the CLI does not cancel the worker.

`resume` retries the original inputs with the original plan/input hash. Changed code
needs a new build or development iteration. Prepared images, cached inputs, matching
core/component outputs and private checkpoints accelerate later runs. Use
`build NEW_LOCK --reuse-from operations/FAILED_RUN` after correcting and resolving
inputs: it transfers the stopped workspace without copying object files. Development
iterations select their previous run automatically. Tests and packaging run again.
`execution.checkpoints` accepts `final` (default), `milestones` or `all`;
`execution.workspace_retention` defaults to `on-failure`, removing compiler scratch
after successful packaging. Set `keep` for repeated development builds. `--no-cache`
disables the legacy shared snapshot cache; it does not disable explicit workspace
reuse, input/toolchain caches or private checkpoints. See [recovery](docs/interfaces/recovery.md),
[development reuse](docs/development.md) and [disk management](docs/development-storage.md).

## Develop core and extension patches

```bash
pg-maker dev open development --lock example/lock.json
# Alternatively: pg-maker dev open development --from-run example/operations/FAILED_RUN
pg-maker dev status development
# Edit the component source path returned by status, then record the patch:
pg-maker dev patch development commit --scope component:yc18-pg-stat-query-plans --id fix-build
pg-maker dev explain development --component yc18-pg-stat-query-plans
pg-maker dev build development --component yc18-pg-stat-query-plans --detach
```

Sessions own Git repositories and support imported history, patch import/export,
refresh, pop/push and rebase. Iterations freeze edits into new locks and remain
unverified. Use the component IDs in the chosen profile. See the [development guide](docs/development.md).

Automation uses the [Python API](docs/interfaces/api.md) or `--machine` JSON output;
`--component-capabilities` lists available commands. Optional pg_play integration
provides MCP tools. A [runtime image handoff](docs/interfaces/runtime.md) can install
locked DEBs into a pinned image for pg_stand without initializing a database.

## Documentation and development

[Documentation index](docs/README.md): task guides, interface contracts, implementation
limits and historical validation reports. Pip installs these manuals under
`<installation-prefix>/share/doc/pg-maker` (`.venv/share/doc/pg-maker` in a venv);
`init` also creates a short project README.

[Contributing and checks](CONTRIBUTING.md) · [Architecture](docs/architecture.md) ·
[Release procedure](docs/releasing.md) · [Changelog](CHANGELOG.md)

Distributed under the [MIT License](LICENSE). Upstream fixtures retain their own notices.
