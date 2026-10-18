# Changelog

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
