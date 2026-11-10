# YC package build, upload and installation

The bundled YC profiles listed in the [profile table](../README.md#choose-and-configure-a-profile)
target Debian 13 **amd64**. This example uses PostgreSQL 18.6. A native ARM64 build needs a
separate platform recipe mapping `aarch64` to Debian `arm64`, ARM compiler flags,
ARM Rust archives. The vectorscale helper supports native x86-64 and AArch64,
applying AVX2/FMA flags only on x86-64. Local ARM builds use a project platform
recipe and pinned ARM Rust archives; there is no bundled ARM profile. Changing only the DEB
filename or target architecture does not port a build.

## Install and initialize

```bash
git clone https://github.com/O2eg/pg_maker.git
cd pg_maker
python3 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
pg-maker init yc18 --profile yc-18.6-icu70
pg-maker validate yc18/build.yaml
pg-maker config show yc18/build.yaml --resolved
```

Linux, Python 3.10+ and a local Docker daemon are required. Match the build UID/GID in
`yc18/config/defaults.yaml` to `id -u`/`id -g`; configure jobs, memory and disk reserve there.
Run on the target architecture. Source development can use `pip install -e '.[dev]'`.
Pip installation itself does not compile PostgreSQL.

All YC profiles initialize these editable settings:

| Setting | File/key | Release value for PG18 |
| --- | --- | --- |
| Vendor | `config/vendor.yaml`, `spec.slug` | `postgresql-yc-clone` |
| Prefix | `config/defaults.yaml`, `spec.paths.runtime_prefix` | `/opt/postgresql-yc-clone/18` |
| PGDATA | same file, `spec.paths.data_directory` | `/var/lib/postgresql-yc-clone/18/data` |
| Unit name | same file, `spec.naming.service` | `postgresql-yc-clone-18` |

Other profiles substitute their major; debug appends `-debug`. Feature IDs such as `yc18-icu70`
select recipes and name branches, not installation directories. Existing projects
keep copied settings when pip is upgraded. Update their YAML and resolve again;
changing a profile does not modify published DEBs or move data.

## Prepare images and build with tests

```bash
pg-maker env prepare yc18/build.yaml --input-store yc18/inputs --out yc18/environments.json --operations yc18/operations
pg-maker resolve yc18/build.yaml --environments yc18/environments.json --input-store yc18/inputs --out yc18/lock.json --operations yc18/operations
pg-maker plan yc18/lock.json --goal package --out yc18/plan.json
pg-maker build yc18/lock.json --plan yc18/plan.json --operations yc18/operations --operation-id build --detach
pg-maker status yc18/operations/build
pg-maker wait yc18/operations/build
pg-maker export yc18/operations/build --out yc18/delivery
```

`env prepare` downloads the base image, installs compilers/dependencies in a temporary
container and saves the prepared Docker image. `environments.json` records image IDs;
the JSON alone does not contain the images. Preparation needs network access.

`build` starts the prepared container itself, mounts the host workspace, sets resource
limits and compiles with networking disabled. No manual `docker run` is needed.
Operation files remain on the host after container removal. Use a new operation ID
for another build.

Tests run by default: core, standard contrib, selected extension source suites and
installed-package checks in fresh Debian 13 containers. Build/test failures stop the
operation; inspect its diagnostics before resuming. Proceed to publication only after
`wait` succeeds. The expensive additional core suites are separate: use the
`test-full` goal, or set `validation.test_suites: [standard, long]` before preparing
and resolving to require them in a package build.

DEBs first appear on the host in:

```text
yc18/operations/build/workspace/output/yc18-icu70.debian13-x64.candidate/packages/
```

Export copies them with reports to:

```text
yc18/delivery/output/yc18-icu70.debian13-x64.candidate/packages/
```

Files: `postgresql-18-yc-clone_18.6-1.deb13_amd64.deb`,
`postgresql-client-18-yc-clone_18.6-1.deb13_amd64.deb`,
`libpq5-yc-clone_18.6-1.deb13_amd64.deb`. `libpq5` identifies the ABI.

Prepared images avoid repeated dependency installation. The input store avoids source
downloads (`resolve --offline` reuses it). Retained workspaces reuse matching component
builds; checkpoints provide fallback recovery. Changes to source, options, libraries
or toolchain invalidate affected outputs. Native compilation and suitable parallelism
reduce elapsed time. See [recovery](interfaces/recovery.md) and
[disk management](development-storage.md) for retention settings. Increase
`spec.product.package_revision` before publishing changed package contents.

## Publish to Nexus

Use a hosted APT repository with the intended suite and signing key and **Disable
redeploy** (`ALLOW_ONCE`). Provide a netrc file with permission `0600` through your
secret store. Save this receiver configuration as `yc18/publication.yaml`, replacing
the example host, repository and credential-file path:

```yaml
api_version: pg_maker/publication/v1
kind: PublicationConfig
spec:
  repositories:
    - id: nexus-debian
      adapter: nexus-apt
      url: https://nexus.example.org
      repository: postgresql-apt
      formats: [deb]
      suite: deb
      component: main
      auth:
        netrc_file: /run/secrets/nexus.netrc
      tls:
        verify: true
```

For an internal CA, add `ca_file: /path/to/corporate-ca.pem` under `tls`.
Publish the export with its test evidence:

```bash
pg-maker publish plan yc18/delivery --config yc18/publication.yaml --out yc18/publication-plan.json
pg-maker publish run yc18/delivery --config yc18/publication.yaml \
  --operations yc18/operations --operation-id publication --detach
pg-maker wait yc18/operations/publication
```

The plan checks local evidence without contacting Nexus. The publication worker
checks repository policy, uploads missing packages and verifies package identity,
APT index entries and downloaded SHA256 values. Matching existing packages are
verified; different bytes under an existing identity are rejected. A successful
upload response alone is insufficient. The exported manifests/reports are required;
a directory containing only DEBs cannot pass the publication gate.

For a single CI command, add `--publish-config yc18/publication.yaml` to the earlier
`build` command. Its returned operation ID then identifies the publisher; wait on
that ID to cover both build and upload failures. See [publication](interfaces/publication.md)
for credentials, multiple receivers, idempotency and resume behavior.

## Explicit faster builds

Select the test policy in `yc18/build.yaml` **before** preparing environments and
resolving. For source-test-free builds that retain installed-package checks, add:

```yaml
spec:
  validation:
    run_db_tests: false
    run_package_tests: true
```

This is a fragment to merge into the existing manifest. Use the same package plan
and build commands above with a new operation ID. Clean-container package checks
still run, and their sealed evidence permits publication. Source qualification
remains `unverified`, `db_tests=skipped` and `publication_eligible=false`; publication
does not turn skipped core/extension source suites into passing evidence.

To omit all database/package tests, set both values to false. With documentation
disabled this also avoids preparing the test image and runtime repository. Build
and packaging integrity checks still run, but **the built-in publisher rejects
these artifacts**. Documentation-only checks do not satisfy the publication gate.
A late `--skip-db-tests` plan option cannot undo dependency preparation and does
not disable explicitly enabled package tests. See the [test-mode matrix](interfaces/build.md#test-modes-and-publication).

## Manual upload alternative

External upload tools operate outside pg-maker's test/evidence policy. Use them only
when deliberately managing publication checks yourself; uploading does not change
the artifact's recorded qualification. Substitute your endpoints and credentials:

```bash
NEXUS=https://nexus.example.org
REPOSITORY=postgresql-apt
NETRC=/run/secrets/nexus.netrc
PACKAGES=yc18/delivery/output/yc18-icu70.debian13-x64.candidate/packages
for package in "$PACKAGES"/*.deb; do
  curl --fail --silent --show-error --netrc-file "$NETRC" \
    --form "apt.asset=@${package}" \
    "$NEXUS/service/rest/v1/components?repository=$REPOSITORY" || exit 1
done
```

For an internal CA, add `--cacert /path/to/corporate-ca.pem`. Publish changed contents
under a new package revision.
Verify Package/Version/Architecture and SHA256 in the APT index and compare downloaded
DEBs against local hashes. A successful POST alone is not this check. This API upload
does not create pg-maker publication evidence.

## Install on Debian 13

Obtain the repository's **public** signing key through a trusted channel. This example
uses an already binary `.gpg` keyring. The suite must match Nexus configuration; it
can be `deb` even though the OS is Debian 13.

```bash
sudo install -m 0644 /path/to/repository-public.gpg /usr/share/keyrings/postgresql-apt.gpg
REPO_URL=https://nexus.example.org/repository/postgresql-apt
SUITE=deb
ARCH=$(dpkg --print-architecture)
echo "deb [arch=$ARCH signed-by=/usr/share/keyrings/postgresql-apt.gpg] $REPO_URL $SUITE main" \
  | sudo tee /etc/apt/sources.list.d/postgresql-apt.list
sudo apt-get update
sudo apt-get install postgresql-18-yc-clone postgresql-client-18-yc-clone libpq5-yc-clone
/opt/postgresql-yc-clone/18/bin/pg_config --version
```

The repository must contain packages for `$ARCH`. Install the corporate TLS CA into
the OS trust store if required; the APT key and TLS certificate serve different
purposes. Private download credentials belong in APT auth configuration.

The service is `postgresql-yc-clone-18.service`. Installation creates an empty PGDATA
but does not initialize/start a database. Initialize or restore the intended cluster
before starting it. Older `/opt/test/...` and `/var/lib/test/...` installations need an
explicit migration plan; package replacement does not relocate data.

Recipe and qualification limits: [PG15](managed-15-icu70.md), [PG16](managed-16-icu70.md), [PG17](managed-17-icu70.md),
[PG18](managed-18-icu70.md).
