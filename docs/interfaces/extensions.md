# Standalone extension packages

`ExtensionBuild` describes a packaged PostgreSQL target and independently versioned
extension recipes. It does not schedule PostgreSQL source compilation or core tests.
The implementation uses MakerService, detached workers, the durable operation journal,
owned Docker resources, InputStore, diagnostic classification and verified exports.

## Target and inputs

The target identifies provider, PostgreSQL major and optionally maintenance version,
OS, architecture, package format, server package and the exact `pg_config` path.
Community targets prepare server/development packages using PGDG. Profile targets
require actual matching database DEBs; a profile name alone is not an SDK. Custom
targets can supply their own packages and prepared base image.

Preparation freezes source archive/Git/snapshot trees, patches, database package bytes,
SDK/header/server hashes, installed PostgreSQL version, native server package version,
build/test image IDs and an offline runtime repository inventory. Compilation has no
network. Repository retrieval occurs during explicit preparation. Image reuse checks
target, dependency closure, selected bundled-module migration, architecture, build
UID and preparation implementation. Runtime dependencies are installed in the SDK
for SQL dependency resolution and source tests as well as saved in the offline
installation repository.
Preparation can inherit standard HTTP/HTTPS proxy environment variables through the
secret environment channel; they are not persisted in images or locks. Provisioning
cache keys exclude changes that only affect compilation, packaging or tests.

The initial bundled recipes are pg_wait_sampling 1.1.11 (SQL version 1.1),
pg_stat_kcache 2.3.1 and pg_repack 1.5.3 for PG18. Other bundled YC majors select
their own existing component versions. User recipes use ComponentRecipe. A recipe
must provide source, supported PostgreSQL versions, install targets, SQL versions
and real tests; the project entry supplies package version and license metadata.
The first standalone builder adapter is PGXS;
unsupported adapters fail before compilation, rather than using guessed commands.
To add another PGXS extension, create a ComponentRecipe and add its recipe path,
upstream version, package revision and license to `spec.extensions`. Dependencies
between selected recipes are ordered; missing providers and cycles are rejected.
Unsupported recipe fields fail explicitly. Native source suites must be declared.

The native package version comes from the extension release, not PostgreSQL's version
or the control file's default_version. Package names include PostgreSQL major and the
profile suffix. Targets and native versions are independent of source version syntax.

## Commands

```sh
pg-maker extension init extensions18 --major 18 --platform debian-13 \
  --extension pg_wait_sampling --extension pg_stat_kcache --extension pg_repack
pg-maker extension prepare extensions18/extensions.yaml \
  --input-store extensions18/inputs --out extensions18/lock.json \
  --operations extensions18/operations --operation-id prepare --detach
pg-maker wait extensions18/operations/prepare
pg-maker extension plan extensions18/lock.json --out extensions18/plan.json
pg-maker extension build extensions18/lock.json \
  --operations extensions18/operations --operation-id build --detach
pg-maker wait extensions18/operations/build
pg-maker export extensions18/operations/build --out extensions18/delivery
```

Without `--detach`, preparation/build commands wait and return the operation's
failure status and nonzero exit code. `--machine` reports the same outcome.
`--request-id ID` makes preparation/build startup idempotent within an operations
directory: repeating the same request returns its operation, while changing its
inputs under the same ID fails. Use a new request ID for a new operation.

For ARM64 use `--architecture arm64` and a matching base image. For RPM use
`--platform rocky-10` (initial validation target) or `rocky-9`. Each target builds inside its own OS; RPMs are
created with rpmbuild, not converted from DEBs. No RPM YC database distribution is
implied by selecting a Debian YC profile.

Use `extension init --profile yc-18.6-icu70 --packages /path/to/database-debs`
for that SDK. Existing YC server/client packages already own kcache/repack files.
By default the build rejects these collisions, including identical file bytes.
The explicit `--split-bundled` migration creates revised copies of the three input
DEBs with `+extensions2.<fingerprint>` appended to their package version and removes only selected
kcache/repack runtime files. Original inputs are untouched; contrib remains complete.
The fingerprint binds the migration revision, original package hashes and selected
modules. Different selections receive distinct native versions. Extension packages
with an exact SDK dependency also include `+sdk.<fingerprint>` in their package
release, preventing different `Depends` from sharing one native package identity.
The output includes the revised database packages, their origin/removal receipt and
the new extension packages. Install this migration set together. It is not a general
replacement for future profile-level split packaging. Builds publish only with an explicit
`--publish-config` option; see [publication](publication.md) for CI/CD and recovery.
Different migration variants are complete alternative sets, not an ordered release
history; do not mix their packages. Select the combined module set when both modules
must be packaged separately. Bump the configured package revision for later releases.

## A user-supplied PGXS recipe

`extension init` copies the bundled recipes. To build another PGXS extension, supply
an `ExtensionBuild` manifest and a `ComponentRecipe` directly. This complete example
uses the same pinned pg_wait_sampling source as the bundled recipe, but does not
depend on the initializer's list of extension names.

Create `custom18/recipes/`, then save `custom18/recipes/pg_wait_sampling.yaml`:

```yaml
api_version: pg_maker/component/v1
kind: ComponentRecipe
metadata:
  id: pg-wait-sampling
  revision: 1
spec:
  type: pgxs
  source:
    kind: archive
    location: https://codeload.github.com/postgrespro/pg_wait_sampling/tar.gz/7e3a5fc937393596c0a51c5881dd63d7208408d1
    sha256: sha256:20538b1543f2004b4bafd43ed8e1de3a50de1aea395fbe81dc9748dc80b982a7
    strip_components: 1
  compatible_postgresql: '>=14,<20'
  requires: []
  conflicts: []
  build_dependencies: []
  runtime_dependencies: []
  make_targets: [all]
  test_targets: [installcheck]
  install_targets: [install]
  required_tests: []
  sql_extension: pg_wait_sampling
  sql_extension_versions: {pg_wait_sampling: '1.1'}
  runtime_files:
    - lib/postgresql/pg_wait_sampling.so
    - share/postgresql/extension/pg_wait_sampling.control
    - share/postgresql/extension/pg_wait_sampling--*.sql
  preload: [pg_wait_sampling]
```

Save `custom18/extensions.yaml`:

```yaml
api_version: pg_maker/extension-build/v1
kind: ExtensionBuild
spec:
  target:
    provider: community
    major: 18
    os: {id: debian, version: '13'}
    architecture: amd64
    format: deb
    base_image: debian:13-slim
    pg_config: /usr/lib/postgresql/18/bin/pg_config
    server_package: postgresql-18
    sdk_packages: [postgresql-server-dev-18]
    runtime_packages: [postgresql-18]
    packages: []
    pgdg: true
    dependency_policy: major
  extensions:
    - recipe: recipes/pg_wait_sampling.yaml
      version: '1.1.11'
      package_revision: 1
      license: PostgreSQL
  execution:
    jobs: 4
    memory_bytes: 4294967296
    min_free_disk_bytes: 5368709120
    stage_timeout_seconds: 3600
    keep_workspace: on-failure
  validation: {run_tests: true}
  maintainer: 'Package Builder <builder@example.org>'
```

Adjust resources to the host, then prepare and build:

```bash
pg-maker extension prepare custom18/extensions.yaml \
  --input-store custom18/inputs --out custom18/lock.json \
  --operations custom18/operations --operation-id prepare
pg-maker extension plan custom18/lock.json --out custom18/plan.json
pg-maker extension build custom18/lock.json \
  --operations custom18/operations --operation-id build
pg-maker export custom18/operations/build --out custom18/delivery
```

Preparation selects the available PGDG server/development packages for that major
and freezes their exact versions and hashes. For a different extension, replace the
source URL/hash, recipe/SQL names, upstream and SQL versions, runtime file patterns,
dependencies, preload requirements and test targets together. Paths in the project
are relative to the manifest; source patches are relative to the recipe. Runtime
patterns describe files under the target installation prefix. The source archive
must contain the PGXS Makefile and the declared test suite; a successful compile
without real tests does not qualify a package. Use `test_setup`, `test_commands` and
`test_settings` when the extension needs additional test preparation or assertions.
See [component recipes](components.md) for shared field meanings; standalone builds
currently accept the PGXS subset described above, not every database-build adapter.

## Dependencies and qualification

Community packages require the target server package, at least the SDK maintenance
version and below the next major. Profile builds require the exact native server
package version. This is a conservative package relationship, not proof of arbitrary
vendor ABI or physical-replication compatibility. SDK and package hashes remain in
the evidence; replacing a database package's bytes under an unchanged native version
does not preserve that evidence.

Control-file SQL dependencies are mapped to selected extension packages or actual
installed providers. ELF dependencies are discovered by dpkg-shlibdeps or native RPM
generators. Explicit runtime dependencies are retained. Private libpq providers are
resolved from the target package database. Extension packages do not start clusters,
modify preload configuration or execute SQL in user databases.

Source tests and isolated package tests run by default. Installed checks use a fresh
matching OS container, no build sources/objects, and only the pinned local runtime
repository. They check missing-server rejection, installed file hashes/ownership,
SQL versions, extension startup/functionality, restart and remove/reinstall with
sentinel data preserved. The three bundled extensions have dedicated functional
checks. Native source suites must report nonzero successful test counts. Skipped
tests produce `unverified`; passing extension qualification does not qualify the
database core. Previous-version upgrade testing is reported separately from reinstall.

Each extension owns a source/object directory and DESTDIR staging tree. A successful
build has a small receipt binding its recipe, SDK, image and staged inventory. Resume
can reuse verified successful stages in the same workspace; failed installs discard
only that component's staging. Packaging and tests run again. Immutable lock changes
require new preparation. Successful runs normally remove objects and test PGDATA;
`execution.keep_workspace: always` retains them for development. Package manifests,
logs and stage timings remain available for export.
For a failed/interrupted preparation or build, obtain `result.input_hash` from
`pg-maker --machine status OPERATION`, then use
`pg-maker resume OPERATION --input-hash sha256:FULL_INPUT_HASH --detach`.
Do not substitute the extension lock/plan hash. See [recovery](recovery.md) for
operation identity and retry rules.
Before recreating a test cluster, PostgreSQL version directories in its tablespaces
are removed using the old cluster's `pg_tblspc` links. Targets must remain inside
the owned build workspace; unrelated files and compiled components are preserved.
Container commands stream stdout/stderr immediately, including partial output before
a timeout or cancellation. Existing locks must be prepared again after controller
changes; this does not authorize editing a frozen lock to resume under different code.

`export --include-inputs` includes frozen extension sources, patches and supplied
database packages. It does not archive the prepared Docker images; retain or
separately transfer those immutable images to replay a lock on another machine.
See the [native validation record](../validation/standalone-extensions-20261110.md)
for the tested platforms and limits.

## Installing the results

| Target | Example pg_wait_sampling package |
| --- | --- |
| Community / Debian 13 | `postgresql-18-pg-wait-sampling_1.1.11-1.deb13_amd64.deb` |
| yc-18.6-icu70 / Debian 13 | `postgresql-18-pg-wait-sampling-yc-clone_1.1.11-1.deb13+sdk.<fingerprint>_amd64.deb` |
| Community / Rocky Linux 10 | `postgresql-18-pg-wait-sampling-1.1.11-1.el10.x86_64.rpm` |

Install through the native package manager with the matching PostgreSQL repository
configured. For an explicit YC migration, install the revised database packages and
extension packages together: `apt install ./target-packages/*.deb ./packages/*.deb`.
For community DEBs use `apt install ./packages/*.deb`; for RPMs use
`dnf install ./packages/*.rpm`. A missing or incompatible server dependency prevents
successful native installation/configuration. No force or dependency bypass is needed.

Enable the preload modules in the intended database instance, preserving any other
required entries, then restart it:

```conf
shared_preload_libraries = 'pg_stat_statements,pg_stat_kcache,pg_wait_sampling'
```

```sql
CREATE EXTENSION pg_stat_statements;
CREATE EXTENSION pg_stat_kcache;
CREATE EXTENSION pg_wait_sampling;
CREATE EXTENSION pg_repack;
```

The pg_repack executable and server module ship together. Its executable is in the
SDK's bindir: `/usr/lib/postgresql/18/bin`, `/opt/postgresql-yc-clone/18/bin` or
`/usr/pgsql-18/bin`, respectively. Existing extension SQL versions require their
normal, separately reviewed `ALTER EXTENSION ... UPDATE` workflow.

RPM packaging validates every ELF runtime search path against the pinned SDK's
absolute library directories before allowing RPM's generic FHS exception for those
paths. Temporary, relative, empty and unrelated paths are rejected. Licenses use
Debian's standard `copyright` location or RPM `%license` files. Test logs survive
failures, and private scratch is returned to the workspace owner before removing the
container. Explicit cleanup also covers the isolated package-test workspace.
