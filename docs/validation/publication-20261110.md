# Publication qualification — 2026-11-10

This record covers the unreleased native-package publisher described in the
[CI/CD publication interface](../interfaces/publication.md). Production Nexus
repositories were not modified during this validation.

## Automated checks

The complete Python unit/contract suite passed **528 tests**. Publication-specific
coverage includes strict receiver configuration, external credential references,
changed/missing evidence, failed or incomplete build gates, native package metadata,
baseline exclusion, filesystem conflicts and symlink rejection, and automatic build
failure propagation through the real CLI.

An isolated local HTTP server implementing the documented Nexus endpoints exercised
APT multipart upload, YUM/Raw PUT, remote checksum/download verification, APT and YUM
index checks, missing/stale/conflicting metadata, and rejection of redeploy-enabled
repositories. A simulated HTTP 503 after committing a package was reconciled without
a duplicate upload. A failed indexing attempt resumed successfully after the index
became available, and repeating the actual CLI request reused the same operation.
These are protocol tests, not qualification against a running Sonatype Nexus service.

Tests also checked source cancellation/resume, preflight of all receivers before the
first write, and private upload snapshots: changing the original package immediately
before the HTTP request did not change the bytes transmitted. Credentials were
resolved at runtime; foreign-origin requests were rejected and values were absent
from saved receiver configuration.

## Real native artifacts

On the native amd64 builder, the publisher consumed existing successful operation
outputs and portable exports with their original test evidence. It transferred and
verified the following artifacts in separate filesystem receiver directories:

| Input | Packages |
| --- | ---: |
| Community PostgreSQL 18.6 / Debian 13, three standalone extensions | 3 DEBs |
| YC PostgreSQL 18.6, three standalone extensions plus bundled-file migration | 6 DEBs |
| Community PostgreSQL 18.6 / Rocky Linux 10, three standalone extensions | 3 RPMs |
| YC PostgreSQL 18.6, pg_repack-only selection plus migration | 4 DEBs |
| yc-15.18-icu70 | 3 DEBs |
| yc-15.19-icu70 | 3 DEBs |
| yc-16.14-icu70 | 3 DEBs |
| yc-16.15-icu70 | 3 DEBs |
| yc-17.10-icu70 | 3 DEBs |
| yc-17.11-icu70 | 3 DEBs |
| yc-18.4-icu70 | 3 DEBs |

All **37 existing packages** passed the publication gate and destination hash checks.
The seven database sets used the explicit package-test-only mode: their sealed
clean-container installation/database checks were accepted without claiming that
source/core tests had run. Original package files and historical qualifications
were not modified.

A separate real CLI run used `extension build --publish-config` with an offline,
prepared Debian 13 SDK. It compiled pg_wait_sampling, pg_stat_kcache and pg_repack,
passed their source tests and clean-container package checks, then automatically
published all three DEBs to a fresh filesystem receiver. The synchronous CLI
returned exit code 0 with a successful publication operation. Build, testing and
publication took **44.3 seconds**, excluding cached SDK preparation.

## Review fixes and revalidation

Completed-source requests now bind the verified publication plan. Tests replace a
successfully published build with different, newly sealed artifacts using identical
build inputs: both direct and automatic publication reject the old request ID.
The worker also rejects a change between request creation and execution.

Export validation checks its inventory and manifest seals, and database manifests
must cover every export branch in the build plan. Tests remove candidate/baseline
branches from live outputs and exports, including a resealed incomplete export.
Exported SDK files are not read by this publication check.

Real HTTP tests delay or truncate metadata and package bodies after response
headers. Verification recovers within its deadline without repeating the upload;
cancellation, size limits and checksum conflicts remain enforced.

After these fixes, six existing amd64 DEBs from PostgreSQL 16.15 and 18.6 portable
exports passed the worker and temporary directory receiver checks. Identical
requests reused their successful operations. A separate ARM export without sealed
package installation tests was rejected. Temporary receiver copies were removed.
The larger builder-side matrix above was not rerun during this follow-up because
SSH to the builder was unavailable.

## Distribution and limits

Ruff, generated-schema stability, wheel/sdist contents, strict Twine checks and an
isolated wheel installation are release checks for this change. The wheel smoke
covers the installed publication CLI, receiver registry and all publication schemas.

No production upload, signing, repository installation/upgrade validation or local
APT/YUM index generation is claimed. Native server-side Nexus behavior still needs
an integration run against the intended test repository with Disable redeploy and
appropriate account permissions. Completed transfers report `publication-verified`,
not `repository-verified`.
