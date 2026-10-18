# Models and schemas

Frozen dataclasses in `models.py` describe commands, environments, operation and
preparation requests, build/runtime/debug requests, environment handles, command
results, outputs, artifacts and cancellation. Do not mutate nested dictionaries;
`start_operation` serializes and validates its own copy.

OperationInput uses `api_version=pg_maker/operation-input/v1`, `kind=OperationInput`
and an `operation_kind` discriminator. Container commands have environment, commands
and outputs. EnvironmentSpec uses an immutable image, numeric UID:GID and target ID.
CommandSpec has a unique ID, argv, cwd, env, secret_env and integer timeout. Cwd is
below `/work/source`, `/work/build` or `/work/output`, without parent traversal.
Required output files use normalized relative paths below `/work/output`.

Preparation requests discriminate `prepare_environments`/`resolve_inputs` and contain
configuration, bindings, output, input store, environment manifest and offline mode.
Build requests contain lock, plan, input store, nullable cache store and optional
`reuse_from`. Runtime requests contain a verified runtime plan. Development command
requests contain the source run/input hash, selected step, argv, cwd and timeout.
These operation kinds also appear in state/event schemas and engine dispatch.

JSON Schema Draft 2020-12 and semantic validation reject unknown fields, duplicate
IDs, ambiguous paths, overlapping public/secret environment variables, NaN/Infinity
and fractional JSON numbers. Durations are integer seconds; elapsed results use
milliseconds. Validation errors include a JSON Pointer without echoing potentially
secret invalid values.

OperationState and BuildRunEvent have separate schemas. The external component
envelope follows the ecosystem contract without internal api_version/kind fields.
Installed schema resources are available through `documents.schema(name)`.

Planning schemas cover PostgreSQLBuild, vendor/defaults/feature/documentation,
platform/component recipes, ChangeSet/TextInsertions, environments, locks and plans.
`tools/generate_planning_schemas.py` regenerates their common definitions. The local
registry resolves package references without HTTP. Optional previous-release inputs
default to an empty list for older effective configurations. Workers verify lock/plan
semantic hashes and the required DAG.

DocRules and TranslationBundle schemas select required fields by action; resolved
assets/bundles become InputStore references with hashes. See [documentation](documentation.md).
New fields require schema and contract updates; incompatible changes require a new
version. A schema-valid document alone is not proof of executed work.
