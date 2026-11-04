from types import SimpleNamespace

import pytest

from pg_maker.documents import validate_document
from pg_maker.errors import ValidationError
from pg_maker.storage import read_json
from pg_maker.validation.diagnostics import classify, compile_rules, preserve_test_logs


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


@pytest.mark.parametrize(
    "message",
    [
        "configure: error: required library not found",
        "/bin/sh: 1: helper: not found",
        "/bin/sh: helper: Permission denied",
        "make[2]: *** [check] Error 127",
    ],
)
def test_tool_failures_are_not_hidden_by_zero_status(tmp_path, message):
    log = tmp_path / "stdout.log"
    log.write_text(message + "\n")
    assert not classify(log, None)["passed"]
    assert not classify(log, None, test_command=True)["passed"]
    # Server logs may contain deliberately failing COPY PROGRAM commands.
    assert classify(log, None, test_command=True, generated_log=True)["passed"]


def test_rules_are_scoped_and_exceptions_preserve_evidence(tmp_path):
    out, err = tmp_path / "stdout", tmp_path / "stderr"
    out.write_text("warning: optional feature\n")
    err.write_text("warning: optional feature\n")
    policy = {
        "warnings_as_errors": True,
        "rules": [
            {
                "id": "optional-feature",
                "pattern": "^warning: optional feature$",
                "severity": "ignore",
                "reason": "This feature is deliberately disabled",
                "stages": ["configure"],
                "tools": ["configure"],
                "contexts": ["build"],
                "channels": ["stdout"],
            }
        ],
    }
    result = classify(out, err, policy=policy, stage="configure", tool="/src/configure")
    assert not result["passed"]
    ignored, error = result["findings"]
    assert ignored["severity"] == "ignore" and not ignored["blocking"]
    assert ignored["reason"] and ignored["rule"] == "optional-feature"
    assert ignored["file"] == str(out) and ignored["line"] == 1
    assert error["blocking"]
    assert classify(out, None, policy=policy, stage="configure", tool="configure")["passed"]
    assert not classify(out, None, policy=policy, stage="compile_core", tool="make")["passed"]


def test_custom_test_rule_overrides_default_negative_test_policy(tmp_path):
    log = tmp_path / "server.log"
    log.write_text("ERROR: division by zero\nPANIC: unexpected recovery failure\n")
    policy = {
        "rules": [
            {
                "id": "unexpected-panic",
                "pattern": "^PANIC: unexpected",
                "severity": "error",
                "contexts": ["test-log"],
            }
        ]
    }
    assert classify(log, None, policy=policy, test_command=True)["passed"]
    result = classify(log, None, policy=policy, test_command=True, generated_log=True)
    assert not result["passed"] and result["findings"][0]["blocking"] is False


@pytest.mark.parametrize("contexts", [None, ["test-log"], ["build", "test-log"]])
def test_tool_scoped_generated_log_rules_are_rejected_before_execution(tmp_path, contexts):
    rule = {
        "id": "unexpected-panic",
        "pattern": "PANIC: unexpected",
        "severity": "error",
        "tools": ["postgres"],
    }
    if contexts is not None:
        rule["contexts"] = contexts
    policy = {"rules": [rule]}
    with pytest.raises(ValidationError, match="explicit build/test contexts"):
        compile_rules(policy["rules"])
    with pytest.raises(ValidationError):
        validate_document(
            {
                "api_version": "pg_maker/diagnostics/v1",
                "kind": "DiagnosticPolicy",
                "metadata": {"id": "probe"},
                "spec": policy,
            },
            "diagnostics",
        )
    log = tmp_path / "server.log"
    log.write_text("PANIC: unexpected recovery failure\n")
    with pytest.raises(ValidationError, match="explicit build/test contexts"):
        classify(log, None, policy=policy, test_command=True, generated_log=True)


def test_preserved_log_rule_blocks_panic_and_allows_command_tool_rules(tmp_path):
    root = tmp_path / "build"
    logs = root / "obj/tmp_check/log"
    logs.mkdir(parents=True)
    (logs / "server.log").write_text("PANIC: unexpected recovery failure\n")
    branch = SimpleNamespace(
        root=root,
        container_root="/work/build/probe",
        output=tmp_path / "output",
        step={"id": "world", "kind": "test_source_world"},
        config={
            "defaults": {
                "validation": {
                    "diagnostics": {
                        "rules": [
                            {
                                "id": "command-only",
                                "pattern": "PANIC:",
                                "severity": "ignore",
                                "reason": "Only a controlled command fixture may print this",
                                "tools": ["postgres"],
                                "contexts": ["test"],
                            },
                            {
                                "id": "unexpected-panic",
                                "pattern": "PANIC: unexpected",
                                "severity": "error",
                                "contexts": ["test-log"],
                                "stages": ["test_source_world"],
                            },
                        ]
                    }
                }
            }
        },
    )
    assert not preserve_test_logs(branch, "/work/build/probe/obj")
    report = read_json(branch.output / "test-logs/world/diagnostics.json")
    assert report["logs"][0]["findings"][0]["rule"] == "unexpected-panic"


def test_diagnostic_rules_reject_duplicate_ids_and_invalid_expressions():
    rule = {"id": "probe", "pattern": "ok", "severity": "info"}
    with pytest.raises(ValidationError, match="duplicate"):
        compile_rules([rule, rule])
    with pytest.raises(ValidationError, match="regular expression"):
        compile_rules([{**rule, "pattern": "["}])
