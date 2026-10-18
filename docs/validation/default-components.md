# Default-component validation — 2026-10-18

New projects select upstream pg_stat_statements, pg_stat_kcache 2.3.2 and
pg_wait_sampling 1.1.11 (SQL version 1.1). Pins/selection rules are in the
[component contract](../interfaces/components.md). This is a historical validation
record, not a claim that every matrix row was rerun for later Python releases.

## Full build

The public init --with-doc → env prepare → resolve → build --goal package --no-cache
--skip-db-tests workflow was exercised for PostgreSQL 18.1 (REL_18_1), Debian 13 /
x86_64, release, LLVM/OpenSSL/ICU and my-feature revision 2.

- Final operation build-final succeeded, 19/19 steps. Core and all three components
  compiled from scratch; external sources used pinned commits. Build/test containers
  had networking disabled.
- Four DEBs were produced: server, client, libpq and docs. Role installation checks
  passed. Server payload contained all three libraries, control/SQL files, the kcache
  dependency on pg_stat_statements and external-component licenses.
- HTML: 1,148 pages and 1,369,668 checked links, without errors. Man: 297 pages.
  PDF: 3,399 pages, with embedded fonts.
- Extension guides appeared as contrib sections F.51/F.52, with sidebar navigation,
  five subsections each, search entries and configured runtime prefix substitution.
- Packaged HTML/search index/PDF matched rendered bytes. Chromium file:// checks
  passed. PDF inspection confirmed long pg_wait_sampling GUC names use lists to
  avoid overlapping table text.

Database regression/TAP/native installcheck, initdb and server startup were not run.
Package checks passed with run_database=false, qualification=unverified and
publication_eligible=false. This validates compilation, packaging and documentation,
not extension runtime qualification.

The isolated test project used jobs=6, a 3 GiB disk reserve and all three formats.
Product defaults stayed jobs=4, 20 GiB reserve, tests off and documentation optional.

## Automated checks

261 maker unit/contract tests passed; eight default-component tests were repeated
after text edits. Twelve pg_play/maker contract tests passed. Configuration coverage
included 14.24/15.1/16.1/17.5/18.1/19beta4, dependencies, standalone wait sampling,
empty components, disabled docs and pinned/changed documentation asset hashes.
Ruff, wheel/sdist, Twine and out-of-checkout installed-resource checks passed.
Declared compatibility is 14–19; this full native component build covered 18.1 only.

## Retained results and cleanup

The local delivery was saved under ~/Desktop/pg_maker-docs-18.1-extensions:
html/index.html and manual.pdf point to the retained copy; build-result contains
exported packages/manuals/reports/logs; build-info contains project/lock/environment,
screenshots, verification and package recheck scripts.

InputStore was not exported. Rebuilding needs source acquisition at pinned refs and
new environment preparation. Absolute paths in old reports describe the finished
scratch run. After export verification, /tmp/pg-maker-default-extensions-20261018 and
two owned image IDs were removed without global prune. Free space on / was about
18.9 GiB at that time; build-info/cleanup.json records measurements and image IDs.
