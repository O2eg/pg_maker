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

Manifest build/validation/execution/naming values override defaults; CLI mode/documentation
options override the manifest. Common changes precede ordered feature ChangeSets.
Required tests are deduplicated while preserving first occurrence; conflicting
acceptance definitions with the same ID are rejected. Documentation rules apply in
profile, feature, component order. Assets and translation bundles enter InputStore
at resolve. Derived provenance uses `derived:config/v1`.

`defaults.validation.run_db_tests` defaults to true for every profile.
The manifest can override it in `spec.validation`; the effective setting enters the
lock. Plan/build test flags affect the plan without changing the lock. Baseline
settings and acceptance expectations remain in configuration, but baseline execution
is omitted when tests are explicitly disabled. Goals `verified` and `test-full`
require tests; normal package builds include fresh-container installation checks.
`defaults.validation.warnings_as_errors: true` makes ordinary build warnings fatal.
`validation.diagnostics` supplies ordered scoped log rules and optional warning
policy; see [build diagnostics](build.md). `validation.test_suites: [standard, long]`
requires long test evidence for packaging as well as source builds. `required_tests`
adds explicit mandatory IDs. These settings enter the lock and build plan.
`packaging.server_dev_files: true` includes the extension SDK in the server package;
the managed profile enables it. No additional package role is created.
`packaging.standard_contrib: true` is enabled in every bundled profile, including
the generic defaults. It delivers all upstream contrib runtime files and requires
OpenSSL, XML/XSLT, UUID, SELinux, Perl and Python options. Missing modules fail the
build even with DB tests disabled. Existing initialized projects must explicitly
update this setting and the required build options, then resolve again.
Selected unsafe warnings and sanitizer findings block without this optional flag.

`execution.checkpoints` is `final` by default; alternatives are `milestones`
(core/contrib boundary plus final) and `all` (eligible completed boundaries plus
final). `execution.workspace_retention` is `on-failure` by default: successful
package/verified goals discard compiler scratch after the final checkpoint; failed
and build-only goals retain it. Set `keep` to retain successful package workspaces
for later `build --reuse-from`. These settings do not disable tests.

Environment preparation omits the test image and offline runtime repository only
when `run_db_tests`, `run_package_tests` and documentation are all disabled in the
manifest. `run_package_tests` defaults to false; source tests imply package tests
for package/verified goals. An explicit true preserves package checks even with
source tests disabled. See the [test-mode matrix](build.md#test-modes-and-publication).
Component `test_dependencies`, `documentation_dependencies` and `extra_source_roles` control
optional inputs. This selection happens before resolution: to enable previously
excluded tests/docs, change the manifest, prepare environments and resolve again.
A late plan/build flag cannot add dependencies absent from an existing lock.

`identity.identities(...) -> dict` computes product version, package revision,
service names, paths, server/client/libpq5 names and filenames. Templates allow only
named placeholders, with no Python evaluation, conversions or recursive expansion.
Feature numeric IDs are unique. Features on the same target cannot share package
names, service names or paths. Coinstallation with the same port is rejected.

`init --profile` accepts `yc-15.18-icu70`, `yc-15.19-icu70`, `yc-16.14-icu70`,
`yc-16.15-icu70`, `yc-17.10-icu70`, `yc-17.11-icu70`, `yc-18.4-icu70` and
`yc-18.6-icu70`. Within each major,
these profiles share component recipes, build options, package revision and test
defaults; only the manifest name, PostgreSQL version and source URL/SHA256 differ.
All YC profiles merge the
bundled `config/yc-clone/{vendor,defaults}.yaml` overlays into the project's editable
`config/vendor.yaml` and `config/defaults.yaml`. Their prefix is
`/opt/postgresql-yc-clone/{postgresql.major}{build.mode_suffix}`, PGDATA is
`/var/lib/postgresql-yc-clone/{postgresql.major}{build.mode_suffix}/data`, and the
service template is `postgresql-yc-clone-{postgresql.major}{build.mode_suffix}`.
The feature ID still selects components, but does not appear in these paths.
Generic projects retain feature-specific directories and services. Existing copied
configuration is not migrated by upgrading pip; edit it and resolve/build again.

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

## Product version format

`spec.product.version_format` selects `three-part` (default, `16.15.0`) or
`postgresql` (`16.15`). The latter requires `release: 0`; use `package_revision`
for package rebuilds. DEB revisions/suffixes still apply, e.g. `16.15-1.deb13`.
An explicit `product.version` must match the computed value. Prerelease identities
retain Debian ordering (`19.0~rc1` with PostgreSQL format). Naming policy v4 is
recorded in identities; server source/version strings are not rewritten.

## Package names by role

`spec.naming.package_by_role` in `build.yaml` overrides individual package-name
templates from `config/defaults.yaml`. Supported keys are `server`, `client`,
`libpq` and `docs`; omitted roles use `naming.package`. Maps merge across the two
files, with the manifest taking precedence. Existing single-template configurations
remain valid, and identities from naming policies v1–v3 remain readable.

```yaml
spec:
  naming:
    package_by_role:
      server: 'postgresql-{postgresql.major}{build.mode_suffix}-yc-clone'
      client: 'postgresql-client-{postgresql.major}{build.mode_suffix}-yc-clone'
      libpq: 'libpq{role.abi_suffix}{build.mode_suffix}-yc-clone'
      docs: 'postgresql-doc-{postgresql.major}{build.mode_suffix}-yc-clone'
```

Names flow into DEB metadata, filenames and internal package dependencies. Service
names and installation paths have their own templates. A naming change requires
resolve/plan/build with a new lock; renaming a finished DEB file does not change
its Package or Depends fields. The unversioned `libpq5-yc-clone` name follows the
libpq ABI; it permits one installed version of that package name per architecture.

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
