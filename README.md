# pg_maker

A Python controller for building PostgreSQL distributions, packages and documentation
in isolated Docker environments. It also provides Git development sessions for
editing patches, recovering failed builds and rebuilding individual extensions.

The current package is an **alpha**. The implemented platform is Debian 13 / x86_64,
with release/debug builds and DEB packages. The PostgreSQL catalog covers majors
14–19, including beta/RC identities; versions below 14 are unsupported. Catalog
coverage is distinct from validation coverage: see the [major-version report](docs/validation/postgresql-majors.md).
RPM, other platforms and distribution repository publishing remain on the
[roadmap](docs/roadmap.md).

## Installation

Requires Linux and Python 3.10+. Container operations require a local Docker daemon;
Git development sessions also require Git. Installing the Python package and reading
capabilities do not require Docker.

From PyPI:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install pg-maker
.venv/bin/pg-maker --help
```

For development, from a source checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pg-maker --help
.venv/bin/pg-maker --component-capabilities
.venv/bin/pg-maker doctor --machine
```

A built wheel can be installed with `python -m pip install /path/to/pg_maker-VERSION-py3-none-any.whl`.

## Build a distribution

```bash
pg-maker init example
pg-maker validate example/build.yaml
pg-maker config show example/build.yaml --resolved
pg-maker env prepare example/build.yaml --out example/environments.json --operations example/operations
pg-maker resolve example/build.yaml --environments example/environments.json --out example/lock.json --input-store example/inputs --operations example/operations
pg-maker plan example/lock.json --out example/plan.json --goal build
pg-maker build example/lock.json --plan example/plan.json --operations example/operations --detach
```

`init` creates editable vendor/defaults/documentation YAML, a `my-feature` variant,
component recipes, schemas and the built-in socket-directory patch. The initial
target is PostgreSQL 18.1 / Debian 13 / x86_64 / DEB, without documentation.
The default components are `pg_stat_statements`, `pg_stat_kcache 2.3.2` and
`pg_wait_sampling 1.1.11`. External sources are pinned by commit SHA. Set
`components: []` to omit additional extensions. See [component recipes](docs/interfaces/components.md).

`env prepare` downloads missing base images and installs dependencies inside
containers. Its result records build/test image IDs, package versions and toolchain
inventory. `resolve` freezes source commits, submodules, snapshots, archive
checksums and ChangeSets in a BuildLock. Offline resolution reuses stored inputs
and reports missing ones. Both operations support `--detach` and `--request-id`.

The build user defaults to `1000:1000` and must match the non-root owner of the
workspace. Environment preparation creates this user inside the image. Build
commands run with networking disabled. Outputs stay inside the operation directory.

**Database tests are disabled by default while developing the utility.** A normal
build compiles the candidate and components without `check`, `check-world`,
component tests, acceptance tests or a baseline build. Reports remain `unverified`,
with `db_tests=skipped` and `publication_eligible=false`. Tests of pg_maker itself
remain enabled.

Use `--goal package` to build server/client/libpq DEBs, manifests, installation
instructions and HTML/JSON reports. A quick package build remains unverified.
The long source/database test adapter currently supports major 18 only:

```bash
pg-maker build example/lock.json --run-db-tests --operations example/operations
# Explicitly requests source and installed-package qualification:
pg-maker build example/lock.json --goal verified --operations example/operations
```

A supplied plan controls the test mode; create it with `plan --run-db-tests` when
needed. `--skip-db-tests` explicitly selects the quick path. Successful source
suites grant at most `source-verified`; `package-verified` requires installed-package
qualification too. See [build](docs/interfaces/build.md) and [packaging](docs/interfaces/packaging.md).

## Build documentation only

```bash
pg-maker init docs-project --with-doc --doc-language en
pg-maker env prepare docs-project/build.yaml --out docs-project/environments.json
pg-maker resolve docs-project/build.yaml --environments docs-project/environments.json --out docs-project/lock.json
pg-maker docs build docs-project/lock.json
```

`docs build` builds documentation without compiling the server or producing DEBs.
It accepts official archive/Git sources or a snapshot of a locally modified source
tree. Set `branding: none` for upstream PostgreSQL branding. The default HTML uses
`sphinx_book_theme`, with an expandable sidebar, page contents and local search.
The footer uses the selected product, version, feature, language and optional vendor
links. `html_backend: docbook` selects the original DocBook layout explicitly.

`config/documentation.yaml` selects HTML/man/PDF. The two external extensions'
guides join the main Sphinx HTML/PDF manual when documentation is enabled.
Russian PostgreSQL output requires a complete, current external translation bundle;
the project's own Markdown documentation is English. HTML/PDF can be packaged in
a separate docs DEB, with manpages assigned to the packages that own their commands.
See the [documentation contract](docs/interfaces/documentation.md).

## Develop code and patches

```bash
pg-maker dev open development --from-run example/operations/FAILED_RUN
pg-maker dev status development
# Edit the source paths returned by status, then record a patch:
pg-maker dev patch development commit --scope component:pg-wait-sampling --id fix-build
pg-maker dev explain development --component pg-wait-sampling
pg-maker dev build development --component pg-wait-sampling
```

Each session owns real Git repositories. It can import an existing local repository
with history, maintain intermediate commits, refresh patches, pop/push a series and
resolve rebase conflicts. New iterations freeze changed inputs into new locks;
`resume` retains its original meaning: retry the same operation with the same inputs.
Independent extensions and the core can be reused when their inputs match. Header,
ABI, toolchain and declared dependency changes invalidate affected results.
[Development guide](docs/development.md) and [interface contract](docs/interfaces/development.md).

## Observe and retain results

```bash
pg-maker status example/operations/RUN --machine
pg-maker events example/operations/RUN --after-sequence 0 --limit 20 --machine
pg-maker wait example/operations/RUN --timeout 120 --machine
pg-maker cancel example/operations/RUN --machine
pg-maker export example/operations/RUN --out exported-result
```

The detached worker owns the operation. Closing a waiting CLI does not cancel it;
cancellation is explicit and repeatable. `status` returns success when it can read
the operation, even if the operation failed; `wait` returns the operation's exit code.
The [Python API](docs/interfaces/api.md) and optional pg_play CLI/MCP integration
use the same service and machine contract.

Source stores, workspaces, checkpoints and Docker caches consume disk space.
Check `df -h`, workspace sizes and `docker system df` before long runs. Use explicit
preview/apply cleanup and preserve needed outputs. See [storage and cleanup](docs/development-storage.md).

## Project documentation

- [Architecture](docs/architecture.md) and [reuse decisions](docs/reuse.md).
- [Roadmap and implementation limits](docs/roadmap.md).
- [Interface specifications](docs/interfaces/README.md).
- [Validation reports](docs/validation/stage56.md), [documentation](docs/validation/stage7.md)
  and [integration](docs/validation/integration.md).
- [Contributing and local checks](CONTRIBUTING.md).
- [Git and PyPI release procedure](docs/releasing.md).

Distributed under the [MIT License](LICENSE). Third-party test fixtures retain their
original copyright and license notices.
