# PostgreSQL 14–19 catalog and boundary validation

Date: 2026-10-17. Contract: [PostgreSQLCatalog](../interfaces/postgresql.md).
The minimum supported major is 14; lower versions fail before execution. Catalog
coverage does not mean every minor/toolchain combination has been tested.

## Scope and full builds

Official release archives 14.24, 19beta4 and 19rc1 were fully compiled on Debian 13 /
x86_64, release, my-feature, LLVM/OpenSSL/ICU, with English DocBook HTML/man/PDF and
server/client/libpq/docs DEBs. Goal was package, shared cache disabled and database
tests disabled. Core/contrib rebuilt each time; prepared toolchain images were reused
by immutable ID. Build, documentation and package installation ran without networking.

Database regression/TAP/installcheck, initdb and server startup were excluded.
Qualification remained unverified and publication eligibility false. This did not
qualify complete Russian translation, all minors, ARM64 or RPM. Majors 15/16/17/18
were checked here at catalog/config/plan level, not as a new full-build matrix.
Earlier 18.1 reports retain their own scope.

| PostgreSQL | HTML pages / links | Manpages | PDF pages | DEBs and installed docs |
| --- | --- | --- | --- | --- |
| 14.24 | 1158 / 28332 | 294 | 3193 | passed, four packages |
| 19beta4 | 1155 / 33296 | 299 | 3295 | passed, four packages |
| 19rc1 | 1155 / 33305 | 299 | 3306 | passed, four packages |

All three succeeded, 18/18 steps. PDFs used embedded fonts; HTML links/assets and
manpages verified. Every role combination installed in a test container with
package-tests.passed=true and run_database=false.

## Documentation-only runs

Both public docs build operations succeeded, 9/9 steps:

| Source | HTML | Man | PDF | Evidence |
| --- | --- | --- | --- | --- |
| Official 14.24 archive, branding none, no common changes | 1158 pages | 294 | 3193 pages | Upstream title/product, no server or DEBs |
| Local 19rc1 snapshot with C and DocBook edits | 1155 pages | 299 | 3306 pages | HTML/PDF marker, C edit in CAS, original tree unchanged |

Plans had configure/documentation steps without compile/install/package/database-test
steps. Absence of server binaries and DEBs was explicitly checked. Isolated local
fixtures inserted PGMAKERLOCALPROOF20261017 into config.sgml and PGMAKERLOCALCODE20261017
into a C header; patch/lock/proof accompanied exports. User source trees were untouched.
See [documentation-only instructions](../interfaces/documentation.md#documentation-only-upstream-or-local-sources).

## Requirements and observed tools

All majors 14–19 use DocBook XML 4.5 despite .sgml filenames. No OpenSP adapter is
needed. Majors 14/15 pass entities into XSLT; 16+ expand postgres-full.xml. C99 applies
through 18, C11 to 19. Upstream does not define one universal minimum GCC version.
YAML records make/Perl/Bison/Flex/LLVM/Meson/options per major; OS/compiler packages
belong to the platform recipe. Meson execution is not implemented.

Observed tools: GCC 14.2.0, LLVM/Clang 19.1.7, Make 4.4.1, Perl 5.40.1, Bison 3.8.2,
Flex 2.6.4, DocBook XML 4.5, XSL 1.79.2, libxml 2.9.14, libxslt 1.1.35, FOP 2.10 and
OpenJDK 21.0.12.1. Environment manifests retain package versions and tool output.

## Defects reproduced and fixed

- The socket patch depended on line 193 in 18.1, versus 210 in 14.24 and 198 in
  19beta4/rc1. Resolve now pins a precise diff from a unique full-line anchor;
  application remains strict without fuzz/offset search.
- Archive timestamps broke PostgreSQL 14 VPATH generation: a newer lwlocknames.h
  yielded an empty .c and missing IndividualLWLockNames. Private source mtimes are
  normalized before changes; CAS stays untouched and later edits still trigger generation.
- 19beta4/rc1 `%S`/`%s` anchors collided after case folding. Version-bounded DocRules
  rename the lowercase anchor to -s-lc, retaining the uppercase release-note link.
  Strict XML/HTML validation remains; rules/diffs enter lock/evidence. Future releases
  do not inherit the fix automatically.
- SVG rendering required libharfbuzz.so.0; Debian PDF dependencies gained libharfbuzz0b.
  A small SVG fixture reproduced failure on the old image and success on the new one.
- SVG Times/Courier and DocBook Symbol bypassed generic font mapping. FOP now supplies
  embedded DejaVu aliases; strict all-font embedding checks remain enabled.

## Automated checks and reproduction

242 unit/contract tests covered catalog boundaries, prerelease DEB ordering, unavailable
zstd on 14, tool minima, patch offsets, VPATH regression and bounded documentation fixes.
Three Docker preparation tests covered cancel/orphan/resume. Seven Docker documentation
tests covered rendering, package installation, translation/Sphinx, recovery and SVG fonts.
These small fixtures supplement the full builds. Ruff/build/Twine and installed-wheel
catalog/schema/dependency/font checks passed.

Archives came from the [official source directory](https://www.postgresql.org/ftp/source/)
with published SHA256 verification:

| Version | tar.bz2 SHA256 |
| --- | --- |
| 14.24 | a7fa7ed3d558172355f51406097a7bd4f6b473be80f311ef7cda96bf383d8897 |
| 19beta4 | 83157ee9c599d03b2f7a3d73ef3a56ec24e0e79cc2b3501a64d1364f56398c86 |
| 19rc1 | af105fc8e960fc836a09fc2cd7f582b49fd17675d9d36631abe3a565bfd7d701 |

To reproduce, choose the exact archive/version/hash with strip_components=1, enable
English HTML/man/PDF, prepare environments, resolve and build package with skip-db-tests.
Changed requirements need new manifests/locks. Init's default remains 18.1, not a beta/RC.
Catalog references were REL_14_STABLE through REL_18_STABLE and REL_19_RC1 at the time;
primary requirements sources are listed in the contract.

## Retention

Five exports were verified before scratch cleanup and saved under
/tmp/pg-maker-major-delivery-20261017. A 19beta4 HTML/PDF example was copied to
~/Desktop/pg_maker-docs-19beta4. Fifteen test-owned images and
/tmp/pg-maker-major-20261017 were removed by exact identity, without global prune.
At that time / had 21 GiB free; delivery occupied 608 MiB and the Desktop example
39 MiB. Other images/temporary directories were untouched. Environment manifests
remain evidence; deleted image IDs require fresh preparation for another execution.
