# Contributing

Use Linux and Python 3.10 or newer. Install the checkout in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python tools/generate_planning_schemas.py
.venv/bin/python tools/check_release.py
.venv/bin/pytest -q tests/unit tests/contracts
```

Review generated schema changes with the matching interface contract. Keep every
project Markdown file in English, including examples and fixture descriptions.
PostgreSQL manuals produced by the utility can use configured languages. Preserve
upstream copyright/license notices in fixtures and generated artifacts.

Public changes need an updated MD contract and, where applicable, JSON Schema.
Use MakerService for CLI/API behavior, typed argv for commands, the shared logger,
durable state/events and owned-resource cleanup. Prefer small concrete adapters;
add protocols only at real replacement boundaries. See [architecture](docs/architecture.md)
and [interfaces](docs/interfaces/README.md).

## Tests and storage

Unit/contract tests do not require PostgreSQL builds. Small Docker tests are opt-in;
variables select already available images. A tiny development pipeline fixture needs
an image with Make and the current non-root build user:

```bash
PG_MAKER_DEVELOPMENT_IMAGE=sha256:FULL_IMAGE_ID .venv/bin/pytest -q tests/integration/test_development_pipeline.py
```

This checks failed extension builds, Git fixes, core/component reuse, debug execution
and retention without starting a database. Other fixtures use PG_MAKER_DOCKER_IMAGE,
PG_MAKER_BUILD_IMAGE, PG_MAKER_PREPARATION_IMAGE, PG_MAKER_PACKAGE_ENVIRONMENTS and
PG_MAKER_DOC_ENVIRONMENTS. CI prepares its own fixture environments through
`tools/ci_pipeline.py --fixtures-only`.

Database suites remain off by default. The expensive PG_MAKER_BUILD_LOCK integration
and manual --run-db-tests path are separate checks, not routine prerequisites for
editing the Python utility. Never treat a successful quick build as database qualification.
Check free disk before work; use [explicit cleanup](docs/development-storage.md) and
retain useful evidence. Do not put runs, inputs, environments, credentials or build
artifacts into Git.

## Distribution checks

```bash
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
.venv/bin/python tools/check_release.py --dist dist
.venv/bin/python tools/smoke_wheel.py dist/*.whl
```

Use a distribution directory containing exactly one version. Release checks compare
all Markdown and runtime package files byte-for-byte between source, wheel and sdist,
and ensure test fixtures/tooling are present in sdist. Wheel smoke creates and removes
a temporary isolated environment, installs dependencies, checks entry points and
initializes/validates a project outside checkout. It does not contact Docker.
Publication is described in [releasing](docs/releasing.md).
