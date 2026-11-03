# Changelog

## Unreleased

- Locked component source patches with immutable original/patched source trees and
  offline patch inputs.
- PLV8 3.2.3: include the standard algorithm declarations required by GCC 14, complete
  interrupted V8 builds, and use the parent make jobserver for nested compilation.

## 0.2.0 — 2026-11-03

- Select the requested architecture from Docker multi-platform image stores during
  environment preparation, including amd64 builds under ARM emulation.
- Configurable two-part PostgreSQL or three-part product versions; package revisions
  remain independent.
- Source-pinned libraries before PostgreSQL configure, exact private pkg-config checks,
  runtime delivery and core/cache invalidation on library changes.
- CMake components, locked auxiliary sources and installed extension-version checks.
- Debian 13 PostgreSQL 16.15 / ICU 70.1 init profile with a complete 38-name managed
  extension catalog, 34 selected SQL extensions from 26 recipes and four explicit
  exclusions.
- Upstream PostgreSQL 16 build fixes for jsquery and pg_cron without changing their
  SQL versions, and the docutils build dependency required by spqrhash installation.
- Public GitHub SSH submodules resolve over HTTPS within public HTTPS checkouts.
- Installed-wheel coverage for the complete named profile, including source pins
  and managed SQL versions. Local build inputs and outputs are excluded from Git.

The amd64 server built under QEMU reports ICU collation version 153.112. The full
extension set and final DEB installation are still being qualified; selected catalog
entries retain their unverified status.

## 0.1.0 — 2026-10-18

Initial alpha of the PostgreSQL distribution controller:

- Strict YAML configuration, pinned sources/environments, immutable locks and plans.
- Isolated Docker builds, durable operations, events, cancellation, recovery and caches.
- PostgreSQL 14–19 catalog; initial Debian 13 / x86_64 / DEB platform.
- Default pg_stat_statements, pg_stat_kcache and pg_wait_sampling recipes with manuals.
- Documentation-only and combined builds using DocBook/Sphinx, local assets, HTML/man/PDF
  and explicit translation bundles.
- Server/client/libpq/docs packaging, manifests, offline installation checks and exports.
- Owned Git development sessions, patch import/refresh/pop/push/rebase, conflict recovery,
  selected component rebuilds, debug commands and iteration retention.
- Optional pg_play CLI/API/MCP integration and runtime image handoff.
- English project documentation, distribution-content checks and a tag-triggered PyPI
  Trusted Publishing workflow.

Database suites are opt-in; quick/development builds remain unverified. RPM, additional
platforms and PostgreSQL distribution repository publication remain future work.
See the [roadmap](docs/roadmap.md) and dated validation reports for precise boundaries.
