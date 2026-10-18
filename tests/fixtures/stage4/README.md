# Stage 4 fixtures

marker.yaml inserts a PostgreSQL 18.1 version() LOG marker when application_name is
pg_maker_acceptance. marker.sql is shared by candidate/baseline: candidate expects
the marker; baseline expects only the named marker assertion to fail.

probe.yaml describes a real C PGXS extension with LOAD, CREATE EXTENSION and
installcheck. source-tree contains the same module for in-tree Make. Utility/library
fixtures exercise Make, program execution and a shared-library function. Compilation
runs exclusively inside the build container.

For the long test, copy this directory to example/fixtures after pg-maker init example,
then configure the feature:

```yaml
spec:
  changes: [../../fixtures/marker.yaml]
  components: [../../fixtures/probe.yaml]
```

Keep the remaining feature fields, choose debug build mode, prepare environments and
resolve. A prepared lock can drive the integration test:

```bash
PG_MAKER_BUILD_LOCK=/absolute/example/lock.json .venv/bin/pytest -q tests/integration/test_build_pipeline.py
```

This is a long compilation/check-world test, separate from utility checks. Inputs and
image IDs must already exist and the lock must match the current implementation.
The built-in free-disk reserve is 20 GiB, configurable through execution limits.
Do not enable database suites merely to validate packaging of the Python utility.
