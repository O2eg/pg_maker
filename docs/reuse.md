# Reuse decisions

Sibling projects informed the design. Their mechanisms were adapted to pg_maker's
contracts; they are not runtime dependencies.

| Reference | Mechanism | pg_maker adaptation |
| --- | --- | --- |
| pg_diag progress | Small progress reporter with UTC, level and percentage | stderr, append-only logs, operation/target/step/attempt context |
| pg_diag artifact IO | Temporary file, fsync, replace and private permissions | Directory fsync; strict JSON rejects duplicate keys and NaN |
| pg_diag errors/security | Common errors and redaction | Stable exit codes, environment secret references, streaming output |
| pg_play state/worker/runner | Worker, flock, journal, cursor and process identity | Owner-lock transfer; journal can reconstruct state snapshots |
| pg_stand Docker runtime | Labels and targeted resource cleanup | Immutable images, networking disabled, stable container paths |
| pg_stand/pg_workload assets | Package resources and repeatable initialization | Check every conflict before writing missing files |
| pg_perf_bench process execution | Separate argv/env/cwd, timeout and process group | Streamed logs and bounded tails |
| pg_converter API/events | Shared service and structured events | Durable journal failures are not swallowed as subscriber failures |
| pg_configurator orchestration | Semantic hashes exclude time and paths | OperationInput hashing excludes local operation directories |
| pg_workload resources | Free disk and available memory checks | cgroup memory limits, CPU affinity and Docker limits |
| pg_diag release workflows | Pinned actions, Python matrix, wheel smoke and documentation checks | pg_maker resources/CLI, explicit long database suites and PyPI release gate |

Mutable object directories are never shared between runs. Reuse restores immutable
snapshots into private directories. The small systemctl test harness is an original,
strict implementation for generated units and reports its lifecycle as emulated.
MCP is provided through pg_play. Workload schedulers, SQL transaction engines and
configuration DSLs do not become part of the build controller.
