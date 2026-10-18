# PostgreSQL major-version catalog

`recipes/postgresql.yaml` is a built-in `PostgreSQLCatalog`, validated by
`postgresql-catalog.schema.json`. The minimum supported major is **14**. Unknown
majors fail before source downloads or environment creation. The catalog describes
upstream requirements; platform recipes map them to OS packages and a toolchain.

`postgresql.version_info(value)` accepts `M.m`, `MbetaN` and `MrcN`, returning
major/minor/stage/number. `19devel` and arbitrary SemVer are unsupported.
`configure.ac` must exactly match the declared version; a beta/RC is never silently
substituted with a stable release.

`postgresql.recipe(version)` merges common fields with the major entry. The result
becomes `EffectiveConfig.postgresql_recipe` and enters lock/plan/cache identity.
Catalog changes require a new resolve. New majors do not automatically inherit the
previous major's behavior.

The catalog records the C standard, tool minimums, generator requirements,
unavailable options, PG_TEST_EXTRA, database test evidence adapter, documentation
format/path/entrypoint, generated prerequisites and make targets. Upstream Meson
availability is separate from implemented pg_maker execution backends.

`verify_tools(minimums, observed)` checks captured container tool output and reports
`precondition_failed` with the tool and minimum. Environment preparation invokes it
before committing the image. LLVM/Clang are checked only with LLVM enabled.
Upstream configure verifies C99/C11 support; a language standard does not imply a
single minimum GCC release. The environment records the actual compiler version.

| Major | C | Upstream make | Perl | Bison | Flex minimum | LLVM | Upstream Meson |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 14 | C99 | 3.80 | 5.8.3 | 1.875 | 2.5.31 | 3.9 | unavailable |
| 15 | C99 | 3.81 | 5.8.3 | 1.875 | 2.5.31 | 3.9 | unavailable |
| 16 | C99 | 3.81 | 5.14 | 2.3 | 2.5.35 | 3.9 | 0.54 |
| 17 | C99 | 3.81 | 5.14 | 2.3 | 2.5.35 | 10 | 0.54 |
| 18 | C99 | 3.81 | 5.14 | 2.3 | unspecified | 14 | 0.54 |
| 19 | C11 | 3.81 | 5.14 | 2.3 | unspecified | 14 | 0.57.2 |

These are catalog values, not a claim that every minor works with every newer
compiler. Flex remains required when its numeric minimum is unspecified. pg_maker
requires make >=4.0 for `--output-sync`. Autoconf 2.69 is listed for configure
regeneration; automatic autoreconf is not performed. Normal Git/release sources
already contain configure. PostgreSQL 14 rejects `--with-zstd`. Meson is recorded
but has no executor. Older minors may require backports or another platform recipe.

All supported majors 14–19 use **DocBook XML 4.5**, despite `.sgml` filenames; an
OpenSP/SGML adapter is unnecessary. Majors 14/15 pass entities directly to XSLT;
from 16, upstream first generates `postgres-full.xml`. The selected source's native
targets are used. HTML/man use xmllint/xsltproc and a local catalog; PDF uses FOP.
Sphinx provides the optional pg_maker presentation layer.

Source references are recorded in YAML: installation/docguide sources, documentation
Makefile, configure.ac, configure and config/programs.m4. Official background:
[14 requirements](https://www.postgresql.org/docs/14/install-requirements.html),
[16 Meson](https://www.postgresql.org/docs/16/install-meson.html),
[19 requirements](https://www.postgresql.org/docs/19/install-requirements.html),
[14 DocBook](https://www.postgresql.org/docs/14/docguide-toolsets.html) and
[19 DocBook](https://www.postgresql.org/docs/19/docguide-toolsets.html).

Stable product identity keeps `M.m.release`. Prerelease DEBs sort as
`19.0.0~beta4-1.deb13` < `19.0.0~rc1-1.deb13` < `19.0.0-1.deb13`.
Prefixes/package roles use major 19, not 19beta4. OCI tags replace `~` with `-`.
Naming policy is v2; identity schemas accept v1/v2. Old locks lacking the catalog or
using a previous implementation identity require a new resolve.

The common socket patch is generated at resolve from one exact full-line anchor
in `pg_config_manual.h`. Its unified diff and exact offset enter CAS and apply
without offset search/fuzz. Zero or multiple anchors fail.

`documentation_fixes(version, recipe)` selects version-bounded DocRules before user
rules. Effective profiles/locks retain full rules; the writer records diffs.
For 19beta4–19rc1, a rule fixes the `%S`/`%s` HTML anchor collision caused by stylesheet
case folding: lowercase `app-psql-prompting-s` gains `-lc`; the uppercase release-note
reference remains. Cardinality is exactly one. The fix is not automatically extended
to 19rc2/19.0; a new release needs validation.

The catalog does not replace acceptance evidence. Database tests default to off;
14–17/19 reject enabling the major-18 evidence adapter as unsupported. Quick builds
remain unverified. Deterministic source mtimes precede ChangeSets as described in
[sources](sources.md). See the [executed matrix](../validation/postgresql-majors.md).
