# Publishing the Python utility

This procedure publishes pg-maker itself. It does not publish PostgreSQL DEBs,
repositories or documentation websites built by the utility. The package is currently
an alpha; version anchors are pyproject.toml and src/pg_maker/__init__.py.

## Repository preparation

The intended repository metadata uses https://github.com/O2eg/pg_maker, following
pg_diag conventions. Before the first push, create or confirm that repository and
check that its name matches pyproject URLs. A local main branch can be initialized
without configuring a remote or creating a release tag. Review the initial file set
and commit it when ready:

```bash
git status --short
git diff --check
# After reviewing the files to publish:
git add .
git diff --cached --check
git diff --cached --stat
git commit -m "Initial pg_maker alpha"
```

The ignore file excludes environments, Python outputs/caches, local secrets and
conventional run/input/development directories. It cannot recognize arbitrary custom
output locations: inspect the staged paths and contents before publication.
Preserve the MIT License and upstream fixture copyright files.

## Validate exact release contents

Update both version anchors and CHANGELOG together. Keep one wheel/sdist version in
the chosen output directory; archive older artifacts elsewhere. From the checkout:

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
python tools/generate_planning_schemas.py
git diff --exit-code -- src/pg_maker/schemas
python tools/check_release.py --tag v0.1.0
python -m pytest -q tests/unit tests/contracts
python -m build
python -m twine check --strict dist/*
python tools/check_release.py --dist dist --tag v0.1.0
python tools/smoke_wheel.py dist/*.whl
```

Substitute the intended version when it changes. The checker validates English Markdown,
local file links, matching versions, exact wheel/sdist documentation and runtime
resources, source test/tooling files, license and exclusion of local/generated data.
The isolated wheel check exercises all three entry points, capabilities, init/validate
and installed manuals outside the source tree. These checks do not start PostgreSQL.

Tests workflow runs Python 3.10/3.12 unit/contracts, schema/lint/docs checks, distribution
validation and small Docker integration fixtures. Full PostgreSQL compilation is a
manual workflow-dispatch job; database tests require an explicit true input. A Python
release does not change PostgreSQL package qualification.

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
git remote add origin git@github.com:O2eg/pg_maker.git  # Only if origin is not configured.
git push -u origin main
git tag -a v0.1.0 -m "pg_maker 0.1.0"
git push origin v0.1.0
```

publish.yml calls the same tests workflow on the tag. The distribution job validates
tag/version equality, builds wheel/sdist once and uploads them. Only after verification
succeeds does the publishing job download those exact artifacts and use PyPI Trusted
Publishing. It does not rebuild them and does not enable long database tests.

Afterward, check remote Actions, the exact PyPI version and installation from the index
in a fresh environment. Record branch commit, tag, workflow outcome and PyPI result
separately. Local checks do not establish remote publication success. A published PyPI
version is immutable; fixes require a new version rather than replacing its files.
