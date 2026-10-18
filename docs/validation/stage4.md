# Stage 4 validation — 2026-10-17

Initial matrix: PostgreSQL 18.1 (REL_18_1), Debian 13, x86_64, Autotools, GCC 14,
LLVM/OpenSSL/ICU and English/Russian NLS. Containers had no network, UID:GID 1000:1000,
4 CPUs, 8 GiB RAM, 1 GiB shared memory and Docker init. The local fixture used a
1 GiB disk reserve; shipped default remained 20 GiB.

Source commit: 4b324845ba5d24682b9b3708a769f00d160afbd7.
Tree: sha256:b56341974fcfceaadd7d730d9dc3876a804e4739eace752af78a06b554806bb3.
Image: sha256:b52b3802cee3840a84b4bf67e547661585e8e9ad64d92bad7c0296709451b237.
Historical workspace: /tmp/pg-maker-stage4-live-20261017.

| Run/check | Result |
| --- | --- |
| Release with common patch | succeeded, source-verified |
| Release make check | 228 regression tests |
| Release check-world | 324 suites: 71 regression, 8 isolation, 245 TAP scripts |
| Release evidence | 676 regression/isolation IDs; 25,190 TAP assertions |
| Not-applicable TAP | 17 policy-declared skip-all suites; 44 individual SKIPs |
| Debug marker candidate/baseline | Both source-verified, 324 suites each |
| Marker acceptance | Candidate marker present; baseline failed only the expected marker assertion |
| Debug PGXS | Installcheck passed in both branches |

These full runs completed before the user requested fast mode by default. Long
PostgreSQL suites were not repeated after that change. Current normal build reports
unverified/db_tests=skipped; full suites require explicit run-db-tests.

After adding protection against all-SKIP suites, saved release stdout/TAP evidence
was reconciled again. Negative checks covered missing/empty suites, unknown skip-all,
invalid plans, missing regression IDs, invalid patches and unresolved required IDs.

Separate live component probes covered PGXS compile/install/LOAD/CREATE EXTENSION/
installcheck; source-tree integration/Make/installcheck; contrib pg_trgm; a compiled
utility/native check; and a shared library called by a test binary. Probe results lived
under operations/debug/workspace/output/component-probe. These were adapter probes
against a private prefix, not a cancelled run relabeled as successful.

## Reproduced defects

1. The common patch used an offset from another version; its 18.1 line was corrected
   without relaxing strict patch application. Later catalog work generalized generation.
2. Successful pg_regress deletes regression.out; evidence now uses complete stdout,
   suite boundaries, names/counts and TAP plans.
3. Without Docker init, recovery/017_shm saw a zombie postmaster as alive after SIGKILL.
   Adding init fixed the suite; orphan-process disappearance was checked in Docker.
4. The initial marker fired during initdb and violated no-stderr assertions. The fixture
   now restricts it to pg_maker_acceptance application_name, preserving the assertion.

Utility/contract/Docker checks covered atomic changes, unchanged original trees on
failure, paths, cancellation/worker loss, idempotent requests and modified-plan rejection.
The long integration test is gated by PG_MAKER_BUILD_LOCK; the full runs above used CLI.

A small real-Docker Make fixture verified quick mode: compile/install/report succeeded,
test targets with deliberate exit 99 were never invoked, no clusters existed and
qualification stayed unverified. Unit checks covered component/baseline/acceptance
omission, flag/config overrides, distinct plan hashes and conflicting saved plans.

After quick mode: 152 passed, one long-suite skip, 58.82 seconds on Python 3.12.
Ruff/format passed. Distributions built and passed Twine. Python 3.10.21 installed-wheel
checks covered dependencies/resources/schemas/build CLI/patches; after the default
change, wheel/init/default-false/CLI switches were checked again.

Stage qualification was source-verified at most, publication eligibility false.
This stage did not validate DEB/InstallTree, installed-product qualification,
runtime repositories, systemctl, package-release qualification or publication.
