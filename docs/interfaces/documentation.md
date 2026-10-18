# Documentation pipeline

CLI → MakerService → BuildPlan/BuildBranch → DockerRunner. Writer/translation libraries
have no independent scheduler; public commands run them inside the pinned build image.
See [stage-7 evidence](../validation/stage7.md) and [full-manual/browser checks](../validation/documentation-theme.md).

## Inputs and configuration

DocumentationProfile (`pg_maker/documentation/v1`) defines enabled, source_language,
languages, formats (html/man/pdf), html_backend (sphinx by default, or docbook),
branding (vendor-metadata or none), rules, translations, glossary, missing_translation
(error or fallback) and require_reviewed. PostgreSQL's source language is English.
Explicit language_aliases normalize inputs while requested_languages retain originals.
Built-ins support en/en-US and ru/ru-RU. Additional languages need an explicit recipe
with tools/fonts. Empty/unknown languages and duplicates after normalization fail
before environment preparation.

With-doc/without-doc override YAML enabled; doc-language replaces the complete language
list and is invalid with docs disabled. Select these before resolve. During build,
they assert consistency with the lock; changes require a new lock. Disabled documentation
keeps the shorter binary-only pipeline.

Rules apply in profile, feature and selected-component order. YAML references are
relative to the containing profile/recipe; fragments/sources are relative to the rules
file. Asset/bundle bytes are pinned by SHA256 in InputStore/BuildLock. Configuration
loading expands copy-tree into ordered copy-file operations for regular files.
Pages/release notes are explicit, never arbitrary automatic extension-file imports.
Default pg_stat_kcache/pg_wait_sampling guides enter the contrib appendix only when
selected. pg_stat_statements uses its upstream page. See [components](components.md).

## Structural writer

`writer.apply(source, destination, documents, context, assets, branding, source_language)`
returns input/output/operation hashes, per-rule results, full unified diff, localized
fragments and validation. It first sets book language and explicit title/product
metadata; copyright notices and code are not rebranded. Rules then run in list order.
A matching verified receipt makes the same clean input idempotent. Reprocessing an
already transformed input is rejected.

DocumentationRules (`pg_maker/doc-rules/v1`) contains metadata ID/revision and rules.
Each rule has ID, action, file, expected match count and optional feature selection.
Structural selectors use ID/tag/parent.

| Action | Additional fields |
| --- | --- |
| insert-before / insert-after / append / prepend / replace | selector, fragment |
| remove | selector |
| set-text | selector, text; element must have no children |
| set-attribute | selector, attribute, text |
| exact-text | old text, replacement and hash of the complete current file |
| copy-file / replace-file / copy-tree | source; expected matches describe destination existence |
| add-page / release-notes | fragment, entity, include_file and unique parent selector |

Add-page/release-notes create a new file, register an entity in filelist.sgml and add
its reference in include_file. Duplicate IDs, missing entities and dangling xrefs
fail the transaction. A prelocalized page can declare language and translate:false;
using it for a different language is rejected.

Declared context placeholders such as ${vendor.brand}, ${vendor.product_name},
${product.version}, ${feature.id} and ${paths.runtime_prefix} are XML-escaped. Process
environment expansion is not supported. Fragments parse independently. Lxml checks
structure without recovery; source token coordinates support precise byte edits.
Untouched files, DOCTYPE, entities, comments and code are not reserialized. Full-book
validation uses DocBook XML 4.5 DTD with local resolution restricted to the source
and installed catalog/ISO entities. Network/arbitrary external files are forbidden.
Expanded books are temporary native-renderer inputs, not writer output.

## Translation bundles

`translation.extract(root, source_language, target_language, context, glossary)`
returns an editable TranslationBundle (`pg_maker/translations/v1`) with source-tree
hash, languages, context, glossary revision/terms and segments. Segments contain ID,
path, structural context, source/hash, placeholders, translation, state and provenance.
States are missing, translated, reviewed, stale and failed.

IDs derive from file/structural position; source hashes include text, protected
fragments, context and glossary. An external translator/import author supplies
translation/state/provenance. Provenance identifies an author or provider/model/options/
prompt revision. Extraction contacts no provider and requires no API credentials or
external database. Bundles are portable import/export files and retain incomplete
segments for continuation. Online provider/retry/budget adapters are not part of the
autonomous build.

Quality checks require the exact tree, languages, feature/target context, segments,
glossary and review policy. Changed text/context is stale. Ordered placeholders protect
XML, entities, SQL/code, parameter names and metadata; deletion/reordering/new placeholders
fail. An unchecked source copy is not a translation. Reports include counts/pages,
missing/stale/failed/reviewed and semantic_accuracy=not-proven.

Apply creates a new tree transactionally from verified segments, preserving IDs,
links and code. The source language is not-required. Missing bundles fail and retain
extracted missing segments in diagnostics. Explicit fallback marks incompleteness;
it cannot bypass stale-tree or reviewed-only checks. Release disallows fallback,
and incomplete documentation cannot grant package-verified.

## Rendering, theme and artifacts

Builder steps are prepare_documentation → per-language translate → per-language build →
export_documentation. Writer follows core changes/source integration; translation
therefore targets the transformed tree. Native Make generates the selected source's
SGML prerequisites. Each language/feature/target has separate trees and objects.
Shared engine infrastructure owns commands/logs, cancellation, limits and checkpoints.

DocBook uses native check/html/man/postgres-A4.pdf targets without external website CSS.
PDF uses local DejaVu fonts, FOP, pdfinfo/pdffonts/pdftotext. Sphinx structurally imports
DocBook pages, preserving anchors/assets and presenting them with sphinx_book_theme.
Book/part/chapter/subsection order is retained. Sidebar expansion is animated with
prefers-reduced-motion support; page sections enter local contents and full text enters
local search. CSS, JS and fonts are local. Under file://, search uses its index without
fetching neighboring-page snippets, which browsers block; HTTP can use snippets.
Keyboard navigation and mobile layout are preserved.

Only validated data generates Python configuration; user executable themes are not
loaded. Sphinx/theme versions enter EnvironmentManifest; templates/CSS/JS/adapter enter
implementation identity. `site.prepare(source, destination, context, language, *,
branding, links)` converts verified DocBook HTML into Sphinx input. Internal .pgdoc
JSON stores title/html/children; sphinx_ext creates semantic section/title/toctree
nodes while retaining body markup. Navigation cycles fail. Titles/text are not parsed
as RST. Site preparation does not itself run Sphinx; the Docker builder does.

Footer templates use product name/version, PostgreSQL version, feature, language and
company. Links appear only from nonempty HTTP(S) vendor.homepage/documentation_url.
Values are escaped. Branding none shows PostgreSQL/upstream version without vendor
metadata/links. Original copyright notices remain. Footers add no support/social links,
analytics or commercial-use restrictions. An older project using docbook must select
sphinx, prepare environments and resolve again; existing locks remain immutable.

DocsArtifactManifest records context, original/written tree, profile, per-language/
format results, tools/image and inventory. HTML checks entry point, language,
brand/version/feature, local links/anchors and offline assets. Man checks pages/aliases;
PDF checks content/fonts. Renderer exit status alone is insufficient.

Install collection verifies files against the manifest and adds them to InstallTree.
HTML/PDF use documentation_directory/LANGUAGE/FORMAT in role docs. Command manpages
belong to the owning binary package; excluded ecpg/unselected utilities have no pages.
Man3 PQ APIs belong to libpq; server SPI/SQL APIs, including dblink, belong to server.
Language paths are runtime_prefix/share/man[/LANGUAGE]/manN. Server only Suggests docs
when enabled; docs does not depend on server. Validation installs docs alone and with
server, reads installed HTML/PDF/man offline without build scratch, and creates no
cluster when database tests are disabled.

## Commands

```bash
pg-maker init project --with-doc --doc-language en
pg-maker env prepare project/build.yaml --out environments.json
pg-maker resolve project/build.yaml --environments environments.json --out lock.json
pg-maker plan lock.json --goal docs --out docs-plan.json
pg-maker docs build lock.json --plan docs-plan.json
pg-maker docs prepare-translations lock.json
sgml-writer lock.json
sgml-translate extract lock.json
sgml-translate apply lock.json
pg-maker build lock.json --goal package --with-doc
```

Standalone commands share lock, Docker engine and operations/detach/machine options.
Docs write returns verified source trees; prepare-translations returns bundles;
apply-translations returns checked translated trees. Docs build emits HTML/man/PDF
and per-language/format tar.xz archives. DEBs require package/verified goals. These
commands neither publish a website nor invoke an online translator.

Adapter references: [PostgreSQL targets](https://www.postgresql.org/docs/18/docguide-build.html),
[lxml parsing](https://lxml.de/parsing.html) and the selected upstream Makefile/stylesheets.

## Major versions and PDF fonts

The lock pins the [catalog](postgresql.md) for majors 14–19. All use DocBook XML 4.5;
the .sgml suffix does not select a legacy SGML pipeline. Recipes declare native
targets/generated prerequisites. Version-specific DocRules precede profile/feature
rules with the same cardinality/validation/diff requirements. Below 14 is unsupported;
see [full-build evidence](../validation/postgresql-majors.md).

Full PDFs include SVG using Times/Courier and FO using Symbol. FOP maps these and
generic families to bundled DejaVu; pdffonts requires every used font to be embedded.
Debian PDF dependencies include libharfbuzz0b for Java's font manager. A tiny text
fixture does not replace SVG or full-manual validation.

## Documentation only: upstream or local sources

Docs build LOCK equals build LOCK --goal docs. It materializes sources, applies
changes/source-tree integration, configures, generates documentation prerequisites,
runs writer/translation/render checks and exports. Core/contrib compilation, prefix
installation, DEBs and database tests are absent. Configure is needed for native
Makefiles/generated tables/version, so provide the complete source tree with configure,
not only doc/. Environment preparation currently uses the common build-image profile,
not a minimal documentation-specific image.

For original upstream documentation, select an official archive with checksum or a
Git ref and configure:

```yaml
# config/documentation.yaml
spec:
  enabled: true
  languages: [en]
  formats: [html, man, pdf]
  branding: none
```

Also clear defaults common_changes, feature changes/rules and extra components for a
pure upstream manual. Version-bounded catalog fixes still apply and appear in writer
diffs. Example archive source:

```yaml
spec:
  postgresql: {version: '14.24'}
  source:
    kind: archive
    location: https://ftp.postgresql.org/pub/source/v14.24/postgresql-14.24.tar.bz2
    sha256: sha256:a7fa7ed3d558172355f51406097a7bd4f6b473be80f311ef7cda96bf383d8897
    strip_components: 1
```

For a modified local tree, use a snapshot:

```yaml
spec:
  postgresql: {version: '19rc1'}
  source:
    kind: snapshot
    path: /absolute/path/to/postgresql-source
    include: ['**']
    exclude: ['.git/**', 'build/**']
```

Snapshots capture selected uncommitted files; Git sources read commits. Explicitly
exclude prior objects/install trees. The version must match configure.ac. The original
directory is never modified. Edits after resolve require a new resolve, otherwise build
uses the previous snapshot. Existing local documentation changes do not require writer
rules; optional branding/rules are separate.

```bash
pg-maker env prepare project/build.yaml --out project/environments.json
pg-maker resolve project/build.yaml --environments project/environments.json --out project/lock.json --input-store project/inputs
pg-maker docs build project/lock.json --operations project/operations
```

Init with documentation enabled creates the starting project; then edit version/source.
Reuse an EnvironmentManifest only when requirements match and image IDs remain local.
Docs-only output consists of selected directories/archives, manifest and reports, without DEBs.
