# Publishing the Python utility

This procedure publishes pg-maker itself. It does not publish PostgreSQL DEBs,
repositories or documentation websites built by the utility. Version anchors are
`pyproject.toml` and `src/pg_maker/__init__.py`.

## Validate exact release contents

Update both version anchors and CHANGELOG together. Keep one wheel/sdist version in
the chosen output directory; archive older artifacts elsewhere. From the checkout:

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
python tools/generate_planning_schemas.py
git diff --exit-code -- src/pg_maker/schemas
python tools/check_release.py --tag v0.4.1
python -m pytest -q tests/unit tests/contracts
python -m build
python -m twine check --strict dist/*
python tools/check_release.py --dist dist --tag v0.4.1
python tools/smoke_wheel.py dist/*.whl
```

Substitute the intended version when it changes. The checker validates English Markdown,
local file links, matching versions, exact wheel/sdist documentation and runtime
resources, source test/tooling files, license and exclusion of local/generated data.
The isolated wheel check exercises all three entry points, capabilities, init/validate
and installed manuals outside the source tree. These checks do not start PostgreSQL.

Tests workflow runs Python 3.10/3.12 unit/contracts, schema/lint/docs checks, distribution
validation and isolated wheel installation on both Python versions, plus small Docker
integration fixtures. Only the Python 3.12 job uploads release artifacts. Full
PostgreSQL compilation is a manual workflow-dispatch job; database tests default to
true and require an explicit false input to skip them. A Python release does not
change PostgreSQL package qualification.

## Configure Trusted Publishing once

Before pushing a release tag, configure PyPI project pg-maker (or a pending publisher
for its first release) and confirm the name is available to this account. Use:

| Setting | Value |
| --- | --- |
| GitHub owner | O2eg |
| Repository | pg_maker |
| Workflow filename | publish.yml |
| GitHub environment | pypi |

Create the matching GitHub environment. No PyPI token belongs in the repository or
build containers. The publish job alone has id-token: write; verification jobs have
read-only repository permissions. Change these values if the actual repository owner
or name differs, including package URLs. Account/project/environment configuration
is external to this checkout and must be completed before the first publication.

## Publish and verify

Only when publication is intended, push the reviewed main branch, then an annotated
version tag matching the package:

```bash
git push origin main
git tag -a v0.4.1 -m "pg-maker 0.4.1"
git push origin v0.4.1
```

publish.yml calls the same tests workflow on the tag. Both Python jobs validate
tag/version equality and build/check distributions; the Python 3.12 job uploads the
release wheel/sdist. Only after verification succeeds does the publishing job download
those exact artifacts and use PyPI Trusted
Publishing. It does not rebuild them and does not enable long database tests.

Afterward, check remote Actions, the exact PyPI version and installation from the index
in a fresh environment. Record branch commit, tag, workflow outcome and PyPI result
separately. Local checks do not establish remote publication success. A published PyPI
version is immutable; fixes require a new version rather than replacing its files.
