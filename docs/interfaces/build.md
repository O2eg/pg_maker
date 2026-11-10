# PostgreSQL and component builds

The initial backend is Autotools on Debian 13 / x86_64, with release/debug builds.
The [catalog](postgresql.md) describes supported majors; the original end-to-end
build target was PostgreSQL 18.1. `MakerService.build` creates a durable BuildRequest.
Source only prepares sources. Without a supplied plan, the default goal is build;
a supplied plan determines the goal unless explicitly overridden. Package, installed
product harness and resume are implemented. Extended release-validation fixtures
remain unsupported; build mode release is independently supported.

## Default tests and test-full

`defaults.validation.run_db_tests=true` is the default for every profile. Standard
builds require core regression, check-world, component and acceptance tests, plus
baseline checks when configured. Package builds also require installation tests in
fresh containers. Unsupported evidence adapters fail before compilation.

`--skip-db-tests`/false is an explicit development override: it omits database tests,
acceptance and baseline, and reports unverified, db_tests=skipped and
publication_eligible=false. `--run-db-tests`/true forces tests. None inherits a supplied
plan or locked default. YAML changes require resolve. The effective boolean enters
BuildPlan and its hash; a flag conflicting with a supplied plan fails before startup.
Source evidence adapters support PostgreSQL 15, 16, 17 and 18. Other catalog majors require
an explicit unverified development build until their adapters are implemented.

For an explicitly requested fast rebuild with installed-package checks, set
`spec.validation.run_db_tests: false` and `spec.validation.run_package_tests: true`
before environment preparation and resolve. Goal `package` then skips source/core/
contrib/extension regression suites and runs the normal isolated DEB installation,
database/extension startup, SDK, remove/reinstall/purge checks. The prepared runtime
repository and test image remain mandatory. Source-test dependencies stay excluded.
The report retains `db_tests: skipped`, `qualification: unverified` and
`publication_eligible: false`; successful package checks are separate evidence in
`package-tests.json`. The default tested build behavior is unchanged.

### Test modes and publication

For a successful database `package` goal, the effective settings have these outcomes:

| Mode | `run_db_tests` | `run_package_tests` | Tests executed | Build qualification | Built-in publication |
| --- | --- | --- | --- | --- | --- |
| Standard, default | true | false or true | Core, contrib, components and installed packages | `package-verified` after all required gates | Allowed |
| Package-test-only rebuild | false | true | Installed packages, including database/extension startup | `unverified`; source tests skipped | Allowed with sealed passing package evidence |
| Untested packages | false | false | No database or installed-database suites | `unverified` | Rejected |

`run_package_tests` defaults to false, but source tests imply package checks for
package/verified goals. `--skip-db-tests` changes only `run_db_tests`; it does not
disable an explicitly enabled `run_package_tests`. Configure these defaults under
`spec.validation` before environment preparation and resolve. Preparation can omit
test dependencies and images only when their consumers are disabled at that time.
Skipping tests later in a plan does not undo already prepared dependencies.

Enabled documentation retains its own checks in every row. Documentation-only
installation checks use `run_database: false` and cannot satisfy the publication
gate. The legacy `publication_eligible` flag stays false when source tests are
skipped; the publisher independently verifies installed-package evidence and hashes.
Publication success reports `publication-verified` without promoting the source
build's qualification. See [publication](publication.md#gate-integrity-and-recovery).

### Expensive source suites

`pg-maker test-full LOCK` (or `build LOCK --goal test-full`) runs all standard source
suites and the expensive core extras: `wal_consistency_checking` for PostgreSQL 15/16/17/18,
and `xid_wraparound` for PostgreSQL 17/18. PostgreSQL 16 has no xid_wraparound module.
`validation.test_suites` defaults to `[standard]`. Selecting `[standard, long]` also
requires the long suites for ordinary build/package/verified goals. `test-full` rejects skipped tests,
exports source evidence, and does not produce packages. `verified` also forces tests;
ordinary tested `package` now provides the same package qualification path.

`validation.required_tests` adds mandatory test IDs to the feature and ChangeSet
requirements. A `source-test-policy.json` receipt binds the selected suites, extras,
required IDs and successful evidence to the lock and plan. Packaging cannot qualify
with a receipt from a different policy/run. Long tests require matching TAP scripts
in the configured inventory, successful results and no skip-all. Baseline remains
explicitly `baseline-core`; candidate requirements are not attributed to baseline.

`execution.build.execute_build(directory, token)` verifies immutable input, lock and
implementation, reconstructs the selected DAG and compares plan hash. Removing a test
step cannot preserve qualification. Branches run sequentially in separate containers
and directories. Candidate receives common plus feature changes; full-test baseline
receives common changes only. Selected components build in both branches.

Full candidate order: sources → changes → integrated source components → configure →
make → private prefix install → expected test inventory → check → world-bin →
install-world-bin → check-world → components/tests → acceptance → report. Baseline
runs core check and component/acceptance checks without check-world, recording
source_test_scope=baseline-core. Acceptance without baseline expectations is candidate-only.

## Diagnostic policy

Configure checks requested settings against Makefile.global/pg_config.h before compile,
rejects unrecognized options and records its result. Every build command has a
structured diagnostic report under diagnostics/. Compiler/runtime errors, sanitizer
findings and selected unsafe compiler warnings block the stage, even with exit 0.
Other warnings are recorded; `validation.warnings_as_errors: true` also blocks ordinary
build warnings. Expected ERROR/WARNING text in negative database tests is recorded
but judged by test outcomes. Generated test logs and regression diffs are copied to
test-logs/ on failure as well as success, with a separate diagnostic inventory. Plans record argv/env/user/cwd,
limits and timeouts; commands generated by native steps are recorded under branch
output/commands.

`validation.diagnostics.rules` adds ordered regex rules scoped by `stages`, `tools`
(executable basename; both accept glob patterns), `contexts` (`build`, `test`,
`test-log`) and `channels` (`stdout`, `stderr`). Missing selectors match every context,
except that `tools` requires explicit `build`/`test` contexts. Generated test logs
have no reliable executable identity: `tools` with `test-log` (including implicit
contexts) is a configuration error. Use stage and message rules for generated logs.
Each rule has a unique `id`, `pattern` and `severity`: `error`, `warning`, `info` or
`ignore`. The last matching rule wins. Ignore requires a `reason`; the finding and
reason remain visible. `validation.diagnostics.warnings_as_errors` overrides the
legacy setting. Nonzero command status and suite reconciliation cannot be waived
by a log rule. A diagnostic-policy change invalidates cached compilation evidence.

Configure errors, shell/make failures and disabled test harnesses are detected even
after exit 0. Generated server/regression logs use `test-log`: expected SQL or shell
errors in negative tests are not infrastructure failures; sanitizers still fail.
Custom rules may enforce a stricter policy for these logs.

Saved logs can be checked without Docker or a build:

```sh
pg-maker logs verify --stdout configure.log --stderr configure.err \
  --stage configure --tool configure --policy diagnostics.yaml --out diagnostics.json
pg-maker logs verify --stdout check.log --test-command --lock lock.json \
  --stage test_source_world --tool make --machine
```

A policy file uses `api_version: pg_maker/diagnostics/v1`, `kind: DiagnosticPolicy`,
`metadata: {id: local}`, and `spec` with `rules` and optional `warnings_as_errors`.
`--test-log` selects generated-log semantics. Exit 0 means the log policy passed;
exit 6 means blocking findings, not proof of a failed or successful test suite.
Reports include file, channel, line, severity, matching rule and ignored reasons.

## Builders and components

Components with `type: library` and `build_phase: before-postgresql` build before
core configure. They require exact `pkg_config_versions`; both version and the
resolved private `libdir` are checked before proceeding. `provides_options: [icu]`
replaces direct platform build/runtime dependencies for that option in this feature.
Other system dependencies may still pull their own ICU transitively. The private
prefix is first in PKG_CONFIG_PATH and LD_LIBRARY_PATH; binaries/libraries receive
its lib directory in RPATH. Source/recipe changes invalidate core and dependent caches.
Library files are inventoried before core install and only declared runtime_files
are packaged. Tested pre-core libraries require explicit test_commands with
`test_cluster: false`; the ICU profile runs its native make check before core configure.
Such test-bearing stages are never satisfied from an ordinary build cache.

External library/utility components may select `build_system: cmake`. Configure uses
an out-of-source directory, private install prefix and RPATH, then bounded parallel
build and install. Explicit test_commands run CTest or the project's own harness;
a zero exit without a nonempty success summary fails. Recipe configure/test arguments
accept `{prefix}`, `{component}`, `{build}`, `{postgresql_source}`,
`{postgresql_build}`, `{jobs}` and `{source:NAME}` for a named extra_sources input. Extra sources use the same pinned resolver, offline store, locks, cache hashes
and portable exports as primary sources; they never download during compilation.

`sql_extension_versions: {vector: '0.8.2'}` verifies installed control files even in
quick mode. It checks package contents, not ABI, index-format or query correctness.
See [the managed profile](../managed-16-icu70.md).

`builders.autotools` contains validate_build, extra_tests, expected_inventory,
reconcile and required-test validation. Queries execute Make inside the container.
A new backend needs a module, explicit dispatch, supported-settings schema and real
source validation, not a plugin framework.

Component integration copies source-tree inputs into unused source directories;
overwriting existing PostgreSQL files requires a ChangeSet. Component build executes
explicit make/install targets, with native/cluster tests only when enabled. Each
component has a separate build_tools or build_and_test_tools step. A quick component
reports built=true and tests=skipped, never passed.

| Type | Build behavior |
| --- | --- |
| contrib | Selected PostgreSQL directory, explicit Make targets and optional load checks |
| source-tree | Separate input copied into an unused source directory, then Make |
| pgxs | Separate sources, USE_PGXS=1 and the exact private-prefix PG_CONFIG |
| utility/library | Make; optional configure with prefix and explicit test targets |

Tested external components must declare test_targets or explicit test_commands. PGXS/contrib/source-tree
REGRESS/ISOLATION/TAP inventory is collected before tests and reconciled afterward;
empty inventory is not success. Build systems are not guessed. SQL extensions,
shared libraries and preload settings are checked in fresh clusters. Internal
headers/PGXS/ecpg support compilation. Packaging may include the extension SDK in
the server role; ECPG binaries/libraries remain excluded.

## Evidence and acceptance

TAP is always configured and cannot be disabled by feature arguments. Full mode
inventories configured SUBDIRS, REGRESS, ISOLATION, schedules and ECPG check.
Check-world remains required for smoke qualification. OpenSSL enables ssl and
libpq_encryption extras. GSSAPI/LDAP/OAuth and the extended release-validation profile
require additional service fixtures and are explicitly unsupported when requested.

TAP::Parser records each Perl script's plan, executed/failed/skipped assertions,
exit/wait and skip_all. Unknown skip-all, empty or missing suites fail. Allowed
not-applicable suites are declared in inventory before execution. Ordinary assertion
SKIPs remain recorded. Successful PostgreSQL removes regression.out, so regression
evidence comes from complete stdout grouped by make --output-sync=target, with
name/count/TAP-plan reconciliation. Original logs, failure diffs and TAP logs remain.
Configure/compiler warnings are recorded per step.

Acceptance uses fresh PGDATA, private-prefix initdb/pg_ctl/psql, bounded readiness
and ON_ERROR_STOP. Configuration and server.log live in PGDATA. SQL scripts are
UTF-8 SQL; command scripts are YAML argv arrays without shell interpretation.
Equals compares complete trimmed stdout; booleans map to psql t/f. Successful scripts
have SQLSTATE 00000. Explicit SQLSTATE assertions may accept psql exit 3 with verbose
stderr evidence; connection errors, timeouts and missing SQLSTATE remain failures.
Prefix substitution works before and after packaging. Baseline assertion-failure
is accepted only for the one named assertion ID.

Required tests resolve to actual evidence IDs: acceptance ID, component.make-target,
source/directory/regression-test or TAP script path. Skipped TAP cannot satisfy them;
unknown IDs fail. Source builds grant at most source-verified and never publication
eligibility. Quick/source-only runs remain unverified. Reports contain receipts,
steps, counts, expectations, warnings and private prefix paths.

## Execution and distribution continuation

The non-root build user must match host mount ownership; environment preparation
creates its passwd/group entries. The prefix is mounted below /opt only inside the
container. Networking is off; memory/swap/CPU/shared memory are limited. Worker
journal/process cleanup/cancellation are shared infrastructure. Keep-failed-environment
retains only a stopped owned container, removable through cancel.

Package uses InstallTree/PackageSpec/DEB and, by default, isolated installation checks
that can grant package-verified. Verified additionally refuses a request to skip tests. Successful matrix branches survive failures elsewhere;
partial reports list failures and return 5. Test steps are not ordinary cache hits.
Command log references are relative to branch output and travel with caches/exports.
See [packaging](packaging.md), [recovery](recovery.md) and [development](development.md).
