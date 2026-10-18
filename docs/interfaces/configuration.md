# Configuration and identity

`config.load_config(manifest_path, config_dir=None, features=None, build_mode=None,
with_doc=None, doc_languages=None) -> EffectiveConfig` reads configuration without
external processes. The result contains data, provenance (JSON pointers to origin)
and bindings (logical inputs to absolute local paths).

`yamlio.load_yaml(path, schema_name=None)` rejects duplicate keys, aliases, unsafe
YAML tags and values outside JSON. Schemas validate unknown fields, API versions,
kinds, integer resources and constraints. Definitions live in
`schemas/planning-common.schema.json`; references resolve from installed package
resources through a local registry, without HTTP.

Configuration directory precedence: CLI, manifest, `PG_MAKER_CONFIG_DIR`, built-in.
A selected custom directory must exist. Vendor/defaults/documentation overlay the
built-ins: maps merge; scalars and lists replace. Features come from that directory
or an explicit path. References resolve relative to their containing file. The
built-in feature is `my-feature`. Platform overrides require an explicit path or
`replace_builtin: true`. Recipes are data, not Python plugins.

Manifest build/validation/execution values override defaults; CLI mode/documentation
options override the manifest. Common changes precede ordered feature ChangeSets.
Required tests are deduplicated while preserving first occurrence; conflicting
acceptance definitions with the same ID are rejected. Documentation rules apply in
profile, feature, component order. Assets and translation bundles enter InputStore
at resolve. Derived provenance uses `derived:config/v1`.

`defaults.validation.run_db_tests` defaults to false during utility development.
The manifest can override it in `spec.validation`; the effective setting enters the
lock. Plan/build test flags affect the plan without changing the lock. Baseline
settings and acceptance expectations remain in configuration, but baseline execution
is omitted when tests are disabled. Goal `verified` requires tests.

`identity.identities(...) -> dict` computes product version, package revision,
service names, paths, server/client/libpq5 names and filenames. Templates allow only
named placeholders, with no Python evaluation, conversions or recursive expansion.
Feature numeric IDs are unique. Features on the same target cannot share package
names, service names or paths. Coinstallation with the same port is rejected.

Flags compose as defaults, mode, feature additions and manifest extra arguments.
CPU flags come from the platform recipe. Extra/feature arguments cannot override
prefix, debug, LLVM, CPU or optimization policy. Release enables FORTIFY; debug
uses O0 without it. Unsupported options, backends, LTO, architectures and formats
fail before environment preparation.

The socket directory follows the common patch and ChangeSet postconditions:
`/var/run/postgresql` with the built-in change, `/tmp` without changes, or a consistent
custom value. Conflicting postconditions produce `validation_error`. Automatic
baseline selection enables it for custom changes; explicit false is invalid when
acceptance includes baseline expectations.

## Previous releases

`spec.previous_releases` optionally supplies packages for same-major upgrade checks.
Paths are relative to `build.yaml`; required hashes are verified during resolve and
build. Each variant appears at most once and includes all three roles:

```yaml
spec:
  previous_releases:
    - variant: my-feature.debian13-x64
      postgresql_version: '18.0'
      packages:
        - {role: server, path: previous/server.deb, sha256: 'sha256:...'}
        - {role: client, path: previous/client.deb, sha256: 'sha256:...'}
        - {role: libpq, path: previous/libpq.deb, sha256: 'sha256:...'}
```

Replace each placeholder with a full SHA256. Previous packages must have matching
names/architecture and a lower native package version. Without an upgrade input,
the installed suite records first-release/not-applicable.

## PostgreSQL major versions

EffectiveConfig includes `postgresql_recipe` from the [catalog](postgresql.md).
Stable `M.m` and prerelease `MbetaN`/`MrcN` versions in majors 14–19 are accepted
subject to platform/feature/component compatibility. Lower and unknown majors fail
before environment creation. The catalog and selected documentation fixes enter
lock/cache identity; `configure.ac` must match the declared version.

Extend the system with versioned schemas and recipe data, then an action adapter
and contract checks. Small functions and `EffectiveConfig` are sufficient; a plugin
framework or dependency injection container is unnecessary.
