from types import SimpleNamespace

import pytest

from pg_maker.builders import native_tests
from pg_maker.builders.autotools import regression_evidence, skip_policy
from pg_maker.errors import CommandError
from pg_maker.validation.upstream.partman import procedure_calls


def test_pg16_regression_output_requires_successful_totals():
    output = """make: Entering directory '/work/build/probe/obj/src/test/regress'
test boolean                      ... ok          12 ms
     int2                         ... ok           4 ms
t/001_recovery.pl .......... ok
======================
 All 2 tests passed.
======================
make: Leaving directory '/work/build/probe/obj/src/test/regress'
make: Entering directory '/work/build/probe/obj/contrib/probe'
/prefix/pg_isolation_regress --inputdir=specs
test locking                      ... FAILED
make: Leaving directory '/work/build/probe/obj/contrib/probe'
"""
    evidence = regression_evidence(output)
    assert evidence["regression", "src/test/regress"] == {
        "tests": ["boolean", "int2"],
        "failed": [],
        "plans": [2],
    }
    assert evidence["isolation", "contrib/probe"]["failed"] == ["locking"]


@pytest.mark.parametrize("count", [0, 5])
def test_native_zero_tests_is_not_a_success(tmp_path, count):
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    out.write_text(f"100% tests passed, 0 tests failed out of {count}\n")
    err.write_text("")
    branch = SimpleNamespace(
        prefix="/opt/probe",
        container_root="/work/build/probe",
        output=tmp_path / "output",
        variant={"build": {"jobs": 8}},
        command=lambda *a, **kw: SimpleNamespace(stdout_log=str(out), stderr_log=str(err)),
    )
    component = {
        "metadata": {"id": "probe"},
        "spec": {
            "test_cluster": False,
            "test_commands": [
                {
                    "id": "ctest",
                    "argv": ["ctest", "--no-tests=error"],
                    "success_pattern": r"0 tests failed out of (?P<count>\d+)",
                }
            ],
        },
    }
    if count == 0:
        with pytest.raises(CommandError, match="nonempty"):
            native_tests.run(branch, component, "/work/build/probe/components/probe")
    else:
        result = native_tests.run(branch, component, "/work/build/probe/components/probe")
        assert result["passed"]
        assert result["native_tests"][0]["reported_counts"] == [5]


def test_partman_intermediate_procedure_is_a_separate_transaction():
    source = """SELECT diag('Run "CALL partman.partition_data_proc(''test.data'', p_wait := 0);".');
SELECT diag('!!! After that, run part2 !!!');
SELECT * FROM finish();"""
    assert procedure_calls(source) == [
        "CALL partman.partition_data_proc('test.data', p_wait := 0);"
    ]


def test_pg16_upstream_disabled_tests_require_matching_source_and_platform():
    recovery = "src/test/recovery/t/031_recovery_conflict.pl"
    source = 'plan skip_all => "disabled due to instability";'
    assert skip_policy(recovery, source, {"MAJORVERSION": "16"}) == "disabled due to instability"
    assert skip_policy(recovery, "", {"MAJORVERSION": "16"}) is None
    assert skip_policy(recovery, source, {"MAJORVERSION": "17"}) is None
    windows = "src/test/modules/test_cloexec/t/001_cloexec.pl"
    source = "plan skip_all => 'test is Windows-specific';"
    assert skip_policy(windows, source, {"PORTNAME": "linux"}) == "test is Windows-specific"
    assert skip_policy(windows, source, {"PORTNAME": "win32"}) is None


def test_rust_toolchain_cache_reuse_and_tamper_rebuild(tmp_path, monkeypatch):
    import json

    from pg_maker.validation.upstream import vectorscale

    root, extra, shared = (tmp_path / name for name in ("component", "extra", "cache"))
    root.mkdir()
    (extra / "cargo-pgrx").mkdir(parents=True)
    for path in (root / "Cargo.lock", extra / "cargo-pgrx/Cargo.lock"):
        path.write_text("package = []\n")
    toolchain = shared / "toolchain"
    calls = []

    def install(argv, **kwargs):
        calls.append(argv)
        (toolchain / "bin").mkdir(parents=True, exist_ok=True)
        for name in ("rustc", "cargo", "cargo-pgrx"):
            (toolchain / "bin" / name).write_text("pinned compiler")

    monkeypatch.setenv("PG_MAKER_RUST_CACHE", str(shared))
    monkeypatch.setattr(vectorscale, "run", install)
    assert not vectorscale.prepare(root, extra, toolchain)
    assert len(calls) == 4
    (shared / "ready.json").write_text(json.dumps(vectorscale.cache_manifest(shared)))
    assert vectorscale.prepare(root, extra, toolchain)
    assert len(calls) == 4
    (toolchain / "bin/rustc").write_text("changed bytes")
    assert not vectorscale.prepare(root, extra, toolchain)
    assert len(calls) == 8
