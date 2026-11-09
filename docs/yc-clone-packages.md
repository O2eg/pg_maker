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

PG16/17 substitute their major; debug appends `-debug`. Feature IDs such as `yc18-icu70`
select recipes and name branches, not installation directories. Existing projects
keep copied settings when pip is upgraded. Update their YAML and resolve again;
changing a profile does not modify published DEBs or move data.

## Prepare the image and build without tests

```bash
pg-maker env prepare yc18/build.yaml --input-store yc18/inputs --out yc18/environments.json --operations yc18/operations
pg-maker resolve yc18/build.yaml --environments yc18/environments.json --input-store yc18/inputs --out yc18/lock.json --operations yc18/operations
pg-maker plan yc18/lock.json --goal package --skip-db-tests --out yc18/plan.json
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

Tests run by default. `--skip-db-tests` applies to the saved plan; build inherits it.
With documentation disabled as in this example, this omits database/component/
installed-package test stages but retains build and packaging integrity checks.
Enabling documentation retains rendering and installed-documentation checks without
starting PostgreSQL. Results remain `unverified`, `db_tests=skipped` and
`publication_eligible=false`; an external upload does not qualify them.

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
downloads (`resolve --offline` reuses it). Shared cache and checkpoints reuse matching
work; changes to source, options, libraries or toolchain invalidate affected outputs.
Native compilation, suitable parallelism and explicitly omitted tests reduce elapsed
time further. Retain images/inputs/cache and budget disk for them. Increase
`spec.product.package_revision` before publishing changed package contents.

## Upload to Nexus

Use a hosted APT repository with the intended suite and signing key, a separately
provisioned netrc file with permission `0600`, and a trusted TLS CA. Substitute your
host, repository and credential-file path:

```bash
NEXUS=https://nexus.example.invalid
REPOSITORY=platform-dba-apt
NETRC=/path/to/nexus.netrc
PACKAGES=yc18/delivery/output/yc18-icu70.debian13-x64.candidate/packages
for package in "$PACKAGES"/*.deb; do
  curl --fail --silent --show-error --netrc-file "$NETRC" \
    --form "apt.asset=@${package}" \
    "$NEXUS/service/rest/v1/components?repository=$REPOSITORY" || exit 1
done
```

For an internal CA, add `--cacert /path/to/corporate-ca.pem`. Replacement of an existing
version requires the repository's redeploy policy; normally publish a new revision.
Verify Package/Version/Architecture and SHA256 in the APT index and compare downloaded
DEBs against local hashes. A successful POST alone is not this check. This API upload
is external to pg-maker's publication eligibility policy.

## Install on Debian 13

Obtain the repository's **public** signing key through a trusted channel. This example
uses an already binary `.gpg` keyring. The suite must match Nexus configuration; it
can be `deb` even though the OS is Debian 13.

```bash
sudo install -m 0644 /path/to/repository-public.gpg /usr/share/keyrings/platform-dba-apt.gpg
REPO_URL=https://nexus.example.invalid/repository/platform-dba-apt
SUITE=deb
ARCH=$(dpkg --print-architecture)
echo "deb [arch=$ARCH signed-by=/usr/share/keyrings/platform-dba-apt.gpg] $REPO_URL $SUITE main" \
  | sudo tee /etc/apt/sources.list.d/platform-dba-apt.list
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

Recipe and qualification limits: [PG16](managed-16-icu70.md), [PG17](managed-17-icu70.md),
[PG18](managed-18-icu70.md).
