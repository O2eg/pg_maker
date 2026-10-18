# Documentation theme validation — 2026-10-17

HTML defaults to sphinx_book_theme. Package templates/CSS/JS/adapter enter implementation
identity; environment manifests pin Sphinx/theme packages. Explicit html_backend=docbook
keeps the original layout. This record describes that day's executed validation.

Environment: Debian 13, Sphinx 8.1.3, sphinx-book-theme 1.1.4-2 and
pydata-sphinx-theme 0.16.1+dfsg-3. Renderer commands ran offline in Docker; dependencies
were installed separately during environment preparation.

| PostgreSQL | Check | HTML pages | Checked links |
| --- | --- | ---: | ---: |
| 14.24 | Theme applied to full upstream DocBook HTML from the previous build | 1160 | 1,394,747 |
| 19beta4 | Full docs-only goal from official archive, 9/9 steps | 1157 | 1,391,312 |

Counts include generated search/index pages. Links, anchors and local assets passed.
19beta4 also produced manpages and a 3,295-page PDF. Core, packages and database tests
were not built in this run; 14.24 PostgreSQL compilation was not repeated.
The [official 19beta4 archive](https://ftp.postgresql.org/pub/source/v19beta4/postgresql-19beta4.tar.bz2)
had SHA256 83157ee9c599d03b2f7a3d73ef3a56ec24e0e79cc2b3501a64d1364f56398c86.
The full example used English, branding none and no common changes.

Chromium file:// checks covered sidebar hierarchy/current section, 220 ms expansion,
keyboard/reduced-motion behavior, page contents/original anchors, indexed local search
without snippet fetches, 390×844 mobile layout without horizontal overflow, and no
JavaScript errors or resource network requests.

EN/RU footers used configured product/version/feature/language/company and only supplied
homepage/documentation links. A separate Docker test checked escaping and anchor
preservation. Heading permalink markers did not enter contents text. Book copyright
notices remained intact.

250 unit/contract tests and eight Docker documentation checks passed, including two
features, EN/RU, HTML/man/PDF, docs DEB and resume. Ruff, distributions and bundled
resource checks also passed.

The updated local example was saved at ~/Desktop/pg_maker-docs-19beta4/html/index.html,
with PDF, screenshots and build-info containing lock/plan/environment/manifest/evidence.
The Desktop copy matched build bytes. Historical paths refer to completed scratch.
Two owned images and /tmp/pg-maker-sphinx-20261017 were removed; / had roughly 20 GiB
free afterward. Exact cleanup evidence is in build-info/cleanup.json.

Older docbook projects must explicitly select sphinx, prepare environments and resolve
again. Locks are not mutated. See [the contract](../interfaces/documentation.md) and
[theme documentation](https://sphinx-book-theme.readthedocs.io/en/stable/).
