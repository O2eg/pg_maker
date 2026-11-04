from types import SimpleNamespace

from pg_maker.storage import read_json
from pg_maker.validation.diagnostics import classify, preserve_test_logs


def test_expected_sql_errors_do_not_hide_test_failures_or_trigger_false_log_failures(tmp_path):
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    out.write_text("ERROR: division by zero\nWARNING: deliberately raised warning\n")
    err.write_text("")
    result = classify(out, err, test_command=True, warnings_as_errors=True)
    assert result["passed"]
    assert result["counts"] == {"test-diagnostic": 1, "test-warning": 1}
    assert not classify(out, err)["passed"]


def test_compiler_and_sanitizer_failures_are_detected_even_after_zero_exit(tmp_path):
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    out.write_text("")
    for text in (
        "file.c:3:5: error: incompatible types\n",
        "file.c:3:5: warning: implicit declaration of function 'unsafe'\n",
        "ERROR: AddressSanitizer: heap-use-after-free\n",
    ):
        err.write_text(text)
        assert not classify(out, err)["passed"]
    assert not classify(out, err, test_command=True)["passed"]


def test_warning_policy_preserves_locations_and_counts(tmp_path):
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    out.write_text("configure: WARNING: optional tool unavailable\n")
    err.write_text("file.c:2: warning: unused variable\n")
    result = classify(out, err)
    assert result["passed"] and result["counts"] == {"build-warning": 2}
    assert result["findings"][1]["channel"] == "stderr"
    assert result["findings"][1]["line"] == 1
    assert not classify(out, err, warnings_as_errors=True)["passed"]


def test_generated_logs_are_retained_and_sanitizer_failures_block(tmp_path):
    root = tmp_path / "build"
    logs = root / "obj/tmp_check/log"
    logs.mkdir(parents=True)
    (logs / "server.log").write_text("ERROR: AddressSanitizer: heap-use-after-free\n")
    outside = tmp_path / "outside.log"
    outside.write_text("must not be copied")
    (logs / "linked.log").symlink_to(outside)
    branch = SimpleNamespace(
        root=root,
        container_root="/work/build/test",
        output=tmp_path / "output",
        step={"id": "core-tests"},
    )
    assert not preserve_test_logs(branch, "/work/build/test/obj")
    saved = branch.output / "test-logs/core-tests"
    assert (saved / "obj/tmp_check/log/server.log").read_text().startswith("ERROR:")
    assert not (saved / "obj/tmp_check/log/linked.log").exists()
    assert read_json(saved / "diagnostics.json")["passed"] is False
