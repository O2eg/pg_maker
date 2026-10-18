# Roadmap and implementation boundary

The numbering follows the ten-stage development plan agreed on 2026-10-12.
Implementation and executed validation are recorded separately.

| Stage | Current state |
| --- | --- |
| 1. Structure and contracts | Package layout, MD interfaces, reuse map and installable CLI implemented. |
| 2. Execution infrastructure | Errors, IO, logging, subprocesses, Docker, worker/state/events/cancel and machine envelope implemented and exercised with real containers. |
| 3. Configuration and inputs | Strict YAML, identities, recipes, sources, ChangeSets, BuildLock and BuildPlan implemented; explicit environment preparation and durable resolve. |
| 4. Build pipeline | Source/build goals, Autotools, release/debug and components implemented. Database suites are opt-in; quick builds remain unverified. |
| 5. Distribution | InstallTree, server/client/libpq DEBs, installation/lifecycle checks, manifests and reports implemented for the initial platform. |
| 6. Recovery and CI | Verified resume, attempts, caches, resource guards and portable exports implemented; workflows prepared. Remote CI status must be checked separately. |
| 7. Documentation | Structural writer, translation bundles, DocBook/Sphinx, HTML/man/PDF, docs DEB and independent validation implemented. No complete Russian manual translation is bundled. |
| 8. Version/platform matrix | YAML catalog for PostgreSQL 14–19, toolchain checks and beta/RC identities implemented. Remaining work: broader minor/toolchain coverage, Ubuntu/Rocky/Alma, ARM64, RPM, tar, OCI and Debian source packages. |
| 9. Integration and development | pg_play CLI/MCP, runtime image handoff, experiment maker phase, ChangeSet workspaces and cleanup implemented. Git sessions add patch stacks, rebase, selected component builds, debug commands, reuse explanations and iteration retention. |
| 10. Distribution publication | Signatures, APT/RPM/Nexus publishing, partial-upload recovery and exact published-version checks remain future work. |

Preparing the Python controller for Git/PyPI release is separate from stage 10,
which concerns publishing the PostgreSQL distributions it builds. See
[the Python release procedure](releasing.md).

## Validation and limits

The implemented target is local Linux Docker, Debian 13 / x86_64 / DEB. Inputs can
be Git, archives or filesystem snapshots. Source/build/package/docs goals work
without database tests. The full database suite result adapter currently supports
major 18. A catalog entry alone is not proof of a completed build; see
[major-version evidence](validation/postgresql-majors.md).

Core and bundled contrib build together. External extensions have separate build
steps and cache entries; a changed extension can reuse the core and independent
extensions. Core/header/ABI changes invalidate dependent extensions conservatively.
Development iterations are unverified, even if a previous release build was qualified.

Resume supports build, resolve and environment preparation. Arbitrary container
commands and debug commands are not automatically replayed. Local runtime repositories
support package checks; the strict systemctl harness does not qualify native
systemd/cgroup behavior. OCI/rootfs environment archive import is not implemented.
There is no standalone pg-maker MCP server; pg_play provides MCP tools.

Remaining requirements include richer requirement-ID evidence, PG_MAKER_* change
context, extended acceptance assertions/fixtures, `build --manifest`, additional
platform adapters, reproducibility analysis and remote execution. HTML run reports
already exist for build operations.

Product decisions: configuration and file logs live in PGDATA; development headers,
PGXS and ecpg are not shipped; there is no separate client installer or devel package;
documentation Suggests is emitted only with documentation enabled. Headers/PGXS
remain available inside the build environment for compiling extensions.

Evidence: [stages 5–6](validation/stage56.md), [stage 7](validation/stage7.md),
[integration](validation/integration.md), [review fixes](validation/review-fixes.md)
and [development/release preparation](validation/development-release.md).
