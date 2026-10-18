# Architecture

`pg_maker` is an independent Python controller. The CLI parses input and displays
results; `MakerService` owns application operations; workers execute durable jobs.
The pg_play MCP adapter uses the same service contract through the machine CLI.
There are no runtime imports from sibling projects.

```text
src/pg_maker/
  cli.py, api.py             CLI and application service
  models.py, documents.py    Typed requests, schemas and semantic validation
  errors.py, storage.py      Stable errors, strict JSON, hashes, atomic IO and locks
  security.py, progress.py   Secret references, redaction and progress logging
  assets.py                  Idempotent project initialization from package data
  config.py, yamlio.py       Strict YAML, effective configuration and provenance
  identity.py                Versions, names and installation paths
  development/              Owned Git sessions, patch stacks, iteration CLI
  execution/                Workers, state/events, cancellation, build and recovery
  runners/                  Runner protocol and Docker implementation
  planning/                 Environment preparation, resolution, locks, DAGs and reuse keys
  sources/                  Git/archive/snapshot resolution and content-addressed inputs
  changes/                  ChangeSets, application receipts and agent workspaces
  builders/                 Autotools and component builders
  packaging/                InstallTree, package roles and DEB output
  validation/               Source, acceptance and installed-package checks
  documentation/            Structural writer, translations, DocBook/Sphinx and theme
  artifacts/                Manifests, output inventory and reports
  integrations/             pg_play/component/v1 machine contract
  schemas/                  Schemas for implemented documents
  bundled/                  Editable files emitted by init
  recipes/                  Platform and PostgreSQL major-version catalogs
```

`execution` contains process streaming and timeouts, a durable event journal,
resource ownership, immutable checkpoints, component caches and disk preflight.
A development iteration creates a new immutable BuildLock/BuildPlan; it does not
change the inputs of an existing operation. Git checkouts remain private to the
session and are separate from build scratch directories.

## Extension rules

Specify inputs, outputs, failure behavior and an MD contract before adding an
adapter. Add contract tests at boundaries that can actually vary. `Runner` is a
protocol because execution backends can be replaced. Built-in implementations use
explicit registries. Ordinary service functions do not need an abstract base class,
dependency injection container or automatic plugin discovery.

Future adapters can extend source resolution, builders, package formats,
qualification and publication. The current executable boundary is documented in
[the roadmap](roadmap.md) and reported by `--component-capabilities`. Placeholder
module directories do not imply that their adapters are implemented.

The build DAG reuses runner/state/process primitives. Infrastructure
`container_commands` requests remain a separate operation type, with their own
schema; they must not silently acquire BuildPlan semantics.
