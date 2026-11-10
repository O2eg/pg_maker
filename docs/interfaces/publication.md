# Native package publication for CI/CD

`pg-maker publish` publishes tested database or standalone-extension packages from a
successful operation or its portable export. Receivers are configured separately
from build inputs. Supported adapters are Nexus **APT**, **YUM**, **Raw**, and an
atomic local **directory** receiver. Package signing and creation of local APT/YUM
indexes are not implemented; Nexus owns its native repository indexes and signing.
Publishing the Python utility uses the separate [release procedure](../releasing.md).

## Configuration

Save an explicit receiver configuration, for example `publication.yaml`:

```yaml
api_version: pg_maker/publication/v1
kind: PublicationConfig
spec:
  request_timeout_seconds: 30
  verification_timeout_seconds: 300
  poll_interval_seconds: 2
  source_timeout_seconds: 86400
  repositories:
    - id: nexus-debian
      adapter: nexus-apt
      url: https://nexus.example.org
      repository: postgresql-apt
      formats: [deb]
      suite: deb
      component: main
      auth:
        username_env: NEXUS_USERNAME
        password_env: NEXUS_PASSWORD
      tls:
        verify: true
        ca_file: /etc/ssl/certs/corporate-ca.pem
```

Provide the referenced variables through the CI secret store. Alternatively use
`auth: {netrc_file: /run/secrets/nexus.netrc}` with private permissions (0600), or
`auth: {bearer_env: NEXUS_TOKEN}` if the server supports bearer authentication.
Omitting `auth` uses anonymous access. Passwords/tokens and URL userinfo are not
accepted as configuration values. Operations persist only credential references;
HTTP response bodies and credential values are not copied into diagnostics.

TLS verification defaults to enabled with system trust. `ca_file` is optional.
A receiver can explicitly set `tls: {verify: false}` for a controlled exception;
it affects only that receiver. Plain HTTP requires `allow_http: true`.
Redirects are rejected; download URLs are constructed from the configured origin.
Relative local directory, CA and netrc paths resolve against the configuration file.

Nexus receivers require a **hosted** repository of the selected format with
**Disable redeploy** (`storage.writePolicy: ALLOW_ONCE`). This closes the race
between independent CI workers checking and uploading the same filename. The
publisher never changes repository settings or replaces conflicting package bytes.
The account needs repository configuration/asset read access and upload access.
For APT, `suite` must match the repository's distribution.

Additional receiver examples (select those needed for the build):

```yaml
# RPM repository; Nexus generates repodata for the repository root.
- id: nexus-rpm
  adapter: nexus-yum
  url: https://nexus.example.org
  repository: postgresql-yum
  formats: [rpm]
  auth: {username_env: NEXUS_USERNAME, password_env: NEXUS_PASSWORD}

# Raw artifact storage, without APT/YUM index verification.
- id: archive
  adapter: nexus-raw
  url: https://nexus.example.org
  repository: postgresql-archive
  formats: [deb, rpm]
  path_prefix: releases
  auth: {netrc_file: /run/secrets/nexus.netrc}

# A filesystem handoff; not an indexed package repository.
- id: pipeline-artifacts
  adapter: directory
  directory: ./delivery
  formats: [deb, rpm]
```

A format can go to several receivers. Every package must match at least one
receiver; receivers without matching packages are configuration errors. YUM
`path_prefix`, when used, must identify a repository root for which Nexus generates
`repodata`. Raw/directory prefixes are ordinary relative subdirectories. APT paths
are determined by Nexus package metadata and cannot use `path_prefix`.

## Separate pipeline steps

```bash
pg-maker --machine --request-id "$CI_PIPELINE_ID-build" build project/lock.json \
  --goal package --operations operations --operation-id database-build

# Read-only local preview: source evidence, package hashes and receiver configuration.
pg-maker --machine publish plan operations/database-build \
  --config publication.yaml --out publication-plan.json

# Synchronous: returns nonzero unless every receiver has been verified.
pg-maker --machine --request-id "$CI_PIPELINE_ID-publish" publish run \
  operations/database-build --config publication.yaml \
  --operations operations --operation-id publication
```

The source can also be an exported operation directory. A plain package directory
or SHA256SUMS file is insufficient: test evidence must accompany the package bytes.
Plan preview performs no HTTP requests and does not resolve credentials; remote
preflight happens in `publish run` before any receiver is changed.

## Automatic publication after a build

```bash
pg-maker --machine --request-id "$CI_PIPELINE_ID" build project/lock.json \
  --goal package --operations operations --operation-id database-build \
  --publish-config publication.yaml

pg-maker --machine --request-id "$CI_PIPELINE_ID-extensions" extension build \
  extensions/lock.json --operations operations --operation-id extension-build \
  --publish-config publication.yaml
```

`--publish-config` explicitly opts into post-build publication. The service starts
the ordinary build and an independent durable publication worker waiting for it.
The returned operation ID belongs to publication; its input and result refer to the
source build. A synchronous command waits for publication, so upload/index failures
fail the CI step. `--detach` returns that operation immediately; the CI system must
later call `wait` and check its exit status. Disconnecting the CLI does not stop
either worker. A machine reboot requires explicit `resume` of interrupted work.

An existing running build can be followed with `publish run SOURCE --wait-for-source`.
Cancelling publication leaves the source build running; cancel that build explicitly
if it should also stop. Failed/partial/cancelled builds cannot publish. Resume the
build first, then resume its publication operation.

## Gate, integrity and recovery

Before upload the publisher validates successful source state, manifests, hashes,
and sealed clean-container package-test evidence. Database package tests must have
started the installed database (`run_database: true`). Skipping source/core tests
is allowed only when these installed-package tests passed; it does not upgrade the
source qualification or historical `publication_eligible` flag. A normal package
build still runs its default tests. Baseline packages are never published, but
failed baseline evidence blocks the matrix. Extension migration DEBs are included
with their matching separately packaged extensions.

Every planned database branch must retain its artifact manifest, including baseline
branches. Portable exports additionally require a valid export inventory, complete
output paths and unchanged input/state/manifest seals. Publication checks package
and test-evidence hashes without rereading exported source trees or SDK archives.

All receiver configurations and filename conflicts are checked before the first
write. Already-present identical files are verified and reused. Different bytes
under an existing package filename fail; use a new native package revision.
Directory receivers publish by atomic no-overwrite linking. Nexus receivers depend
on the server's no-redeploy policy as well as checksum checks. Publication is not
transactional across receivers: successful earlier uploads are retained on failure.

APT uses the Nexus Components API (`apt.asset`); YUM/Raw use PUT. The publisher
waits for the native index to list the exact package/hash, then streams the package
back and compares size and SHA256 with the tested input. YUM primary metadata is
also checked against its repomd checksum. Raw/directory receivers verify bytes
without claiming indexed repository availability. These protocols follow the
[Sonatype upload API](https://support.sonatype.com/hc/en-us/articles/115006744008-Repository-How-can-I-programmatically-upload-files-into-Nexus-3)
and [APT repository documentation](https://help.sonatype.com/en/apt-repositories.html).

A timed-out write may already have committed. The publisher checks remote state
before any later upload attempt. It does not blindly retry a POST. Resume verifies
all remote objects again, including previously completed ones, and continues
missing uploads without rebuilding PostgreSQL:

```bash
pg-maker --machine status operations/PUBLICATION_ID
pg-maker --machine resume operations/PUBLICATION_ID --input-hash sha256:ORIGINAL_HASH
```

Keep the operation workspace and source artifacts available between CI attempts.
`--request-id` deduplicates identical requests within an operations directory and
rejects changed inputs. `resume` requires the original input hash and unchanged
source/configuration. Credential values can rotate behind unchanged references.
For completed sources, the request also binds the verified publication plan and
package hashes. Automatic publication seals its plan when the source finishes;
repeating a completed automatic request checks the current artifacts against that
plan before returning the previous success. Replacing artifacts while retaining
the same build inputs cannot reuse a successful publication result.

Timeouts, connection resets and truncated HTTP bodies during remote verification
are retried within the verification deadline. Size limits and checksum conflicts
remain errors; retrying verification does not repeat a package upload.

`workspace/output/publication-plan.json`, `publication-result.json` and individual
receipts record destinations, hashes, upload/reuse actions and index verification.
The public API exposes `MakerService.publication_plan(source, config)` and
`MakerService.publish(source, config, wait_for_source=False, request_id=...)`;
`build` and `extension_build` accept `publish_config=Path(...)`.

A completed upload has qualification `publication-verified`. This is **not**
`repository-verified`: installation via the final repository, GPG trust-chain
validation, and published-version upgrade tests are separate future work. No source
or installed-package test qualification is inferred merely from upload success.
