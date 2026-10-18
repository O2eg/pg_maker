# Development workflows and Python release preparation — 2026-10-18

This validation concerns the controller and its development/release interfaces. It
does not claim a new PostgreSQL matrix qualification or a remote publication.

## Development behavior

The local Python 3.12 check passed 272 unit/contract tests and one real Docker pipeline
fixture (273 total), repeated on the final code in 75.97 seconds. Thirteen pg_play
maker/development contract tests also passed. The fixture uses tiny Make sources,
never PostgreSQL or initdb.
It reproduced a failed extension build, exact-input resume, opening the failed run in
a separate Git session, committing a fix, reusing the core checkpoint and independent
extension, retaining useful partial objects, successful rebuilding, debug execution
against both rebuilt/cached components and preview/apply retention. Additional real-Git
checks cover middle-patch refresh, pop/push, history-preserving local repository import,
conflicted multi-patch import/rebase continue/abort and already-upstream patches.

Compilation keys are separate for core and external components. Core/header/ABI changes
conservatively invalidate affected dependencies; bundled contrib remains part of the
combined core build. Development locks remain unverified. InputStore collection and
native database qualification are outside these checks.

## Release preparation

All owned Markdown was converted to English and updated against current behavior.
The release checker verified all 47 documents, their local file links and matching
version 0.1.0. Ruff lint/format and schema generation passed. Wheel and sdist built
successfully and passed strict Twine validation; documentation, runtime resources,
license, source fixtures and tooling matched the checkout byte-for-byte.

An isolated Python 3.12 wheel installation outside checkout passed dependency checks,
all three CLI entry points, capabilities, project initialization/validation and
installed-manual checks. Negative checks confirmed rejection of a mismatched release
tag and a wheel missing its development contract. Python 3.10 is included in the CI
matrix; it was not rerun locally during this preparation.

The repository includes consistent version/metadata/license, ignore rules, contribution/
release guidance, archive-content verification, isolated-wheel smoke and CI/PyPI workflows.
PyPI publication is tag-triggered and requires externally configured Trusted Publishing.
Local Git was initialized on main without a remote or initial commit. No remote
repository, release tag or PyPI upload was created. Remote Actions was not executed.

The tiny Docker fixture image/context were removed after tests. Global Docker prune
was not used; unrelated containers/images were retained. The two Python distributions
occupy less than 1 MiB together; free space on / remained about 19 GiB.

Historical PostgreSQL build reports retain their original dates and scope. Database
suites remain disabled by default. The release checklist is in [releasing](../releasing.md).
