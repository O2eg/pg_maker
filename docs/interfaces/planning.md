# BuildLock and BuildPlan

Planning and execution support source/build/package/verified and documentation goals.

`planning.resolve.resolve_inputs(PreparationRequest, context) -> dict` runs in the
worker after OperationInput is durable. It verifies EnvironmentManifest and the
requirements hash, copies source/component/ChangeSet inputs into InputStore, checks
`AC_INIT([PostgreSQL], [VERSION], ...)` and returns BuildLock. A file changing between
YAML loading and copying is a checksum error. Patches, acceptance scripts and the
built-in change have separate blob IDs. Mutable source locations become immutable
source descriptions.

`planning.plan.build_plan(lock, goal='build', input_store=None, run_db_tests=None,
components=None) -> dict` verifies schema, lock hash, implementation identity, trees
and blobs, then creates a DAG. Planning does not compile, install, create caches/logs
or contact the network/Docker. Only explicit CLI `--out` writes a plan. The executor
checks local image availability and runnability before execution; planning checks
the immutable IDs and manifest stored in the lock.

`components` selects components and transitive `requires` dependencies for goal
`build` with database tests disabled. Each external component has its own step and
stage key. Core keys exclude external PGXS recipes and documentation rules.
Component keys include core inputs, their own compilation inputs and dependency
keys. Other steps conservatively include the full lock identity. Test steps use
`cache: never`.

`BuildLock.spec.development` records session ID, base/parent lock, Git HEAD/base/series
and source snapshot for each scope. Input hashes are verified normally. Development
locks cannot be used for full release qualification.

Goals form source → build → package → verified. Documentation goals are `doc-source`,
`translations`, `translated-docs` and `docs`; they require enabled documentation,
configure sources and generate tables without compiling/testing the database.
With documentation enabled, ordinary builds include its outputs and InstallTree.
Each feature × target has a candidate branch. A baseline branch uses upstream plus
common changes only, when baseline and database tests are enabled. Baselines are
not publishable distributions. The implemented platform uses DEB and Autotools.

`spec.run_db_tests` is part of the plan hash. Build/package inherit the lock default
when no override is supplied; the development default is false. Source always has
false. Verified requires true and rejects an explicit false. The default CLI plan
goal is build. Without database tests, inventory/test/acceptance steps and baseline
are absent; component build/install remains enabled.

StepSpec defines id/kind/adapter, dependencies, feature/target/branch, environment
and image IDs, argv/env/user/cwd, parameters, inputs/outputs/checks, timeout,
resources, cache/recovery and stage key. Cwd/output PathRefs use
`{root: source|build|output, path: relative}`. Container argv uses stable `/work`
paths, not host paths. Native actions have empty argv and explicit parameters.
`execution_available=true` indicates an implemented executor, not execution evidence.

`seal(name, spec, transport)` and `verify(document, name)` implement
`pg_maker/environment/v1`, `pg_maker/lock/v1` and `pg_maker/plan/v1`. Canonical UTF-8
JSON containing API version, kind and spec is hashed; the hash itself, creation time
and transport are excluded. Moving InputStore does not change lock/plan hashes.
Implementation identity includes package version and installed code, schemas, YAML,
patches and package templates. Changed implementation requires a new resolve.
A hash is not a document signature.

Errors: `validation_error` for schema/version/DAG problems; `precondition_failed`
for missing/changed inputs or stale environment/code; `unsupported` for unavailable
features. A failed worker does not publish a new completed lock. Verified blobs
remain reusable. Preparation resume requires the exact input hash. See [recovery](recovery.md).
