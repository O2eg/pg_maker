# Stage 7 validation — 2026-10-17

Local environment: Linux x86_64, Python 3.12, Docker and Debian 13 build/test images.
PostgreSQL and installed-database suites were disabled. Documentation make check
validates DocBook and does not run database regression/check-world.

Implemented: structural writer, externally populated translation bundles with strict
freshness/completeness checks, offline application, DocBook/Sphinx HTML/man/PDF, docs
DEB, command-owned manpages, recovery and portable export. See [the contract](../interfaces/documentation.md).

## Executed checks

- Final unit/contracts: 191 passed. Before two last regressions, the combined run was
  217 passed/one opt-in long-suite skip, including 28 small Docker tests. New regressions
  covered CLI documentation selection and server/libpq man3 ownership. Full PostgreSQL
  was built separately through CLI.
- PostgreSQL 17.5/18.1 DocBook fixtures: real DTD/catalog, nested entities, exact golden
  edits, preserved code/comments/IDs, new pages/escaping, rejected external entities,
  dangling references and incorrect cardinality.
- DocBook HTML/man/PDF and Sphinx HTML for English/Russian and two features. A small
  reviewed Russian bundle was imported explicitly; both expected translated headings
  appeared in extracted PDFs with embedded fonts. This was not a full manual translation.
- Incomplete/stale bundles, missing placeholders and unchanged source copies failed.
  Fallback remained disabled by default.
- Renderer worker SIGKILL, resource cleanup, checkpoint restore and independent parallel
  run; unfinished output was not reused.
- Offline standalone docs-DEB installation without server/build mounts; client owned
  psql.1 and server only suggested docs. An additional installed-PDF test passed.
- Full PostgreSQL 18.1 writer checked 5,140 IDs; native targets produced 1,144 HTML pages
  and 297 manpages. 32,557 links, metadata and local assets verified.
- Actual man3 inventory exposed ownership errors: server owns SPI/dblink, libpq owns
  PQ APIs. A regression covers SPI_connect, dblink, PQconnectdb, psql and excluded ecpg.
- Distributions/Twine, Python 3.10 offline installed CLI/init/validate, Ruff and reproducible
  schemas passed. Exports of both-language Sphinx and PDF-package fixtures had all
  artifact/evidence hashes rechecked.

Full package-doc-ownership succeeded, 18/18 steps and empty resources. Libpq/client/
server/standalone-docs installation sets all passed. Payload, Suggests without docs
Depends, manpage roles and ecpg exclusion were rechecked. Export hashes passed.
Qualification remained unverified, database tests skipped and publication ineligible.

| Role | Package | Bytes |
| --- | --- | ---: |
| server | test-my-feature-server-18 | 14,065,620 |
| client | test-my-feature-client-18 | 818,568 |
| libpq | test-my-feature-libpq5-18 | 146,244 |
| docs | test-my-feature-docs-18 | 2,053,912 |

Historical evidence:

```text
/tmp/pg-maker-stage7-delivery/
  postgresql-18.1/       # Four real DEBs and full English HTML/man
  sphinx-fixtures/       # Two features, EN/RU, HTML/man/PDF
  docs-package-fixture/  # Standalone HTML/PDF DEB installation
/tmp/pg-maker-stage7-tests-final.log
/tmp/pg-maker-stage7-unit-final.log
/tmp/pg-maker-stage7-pdf-package-test.log
/tmp/pg-maker-stage7-py310.log
/tmp/pg-maker-stage7-native-summary.json
/tmp/pg-maker-stage7-python-dist/
```

No complete Russian translation or online provider/retry/budget adapter is bundled.
Structural completeness does not prove linguistic accuracy. Full upstream PDF was
not rendered in this stage; FOP/fonts/install were checked on small fixtures. Later
major-version reports cover full PDFs. This stage did not qualify PostgreSQL 17,
RPM/ARM64 or native systemd. Remote CI was not run; workflows include documentation
fixtures and manual with_doc, with database tests off by default.

Follow [storage guidance](../development-storage.md): export evidence before removing
large completed workspaces. Global Docker prune is never automatic.
