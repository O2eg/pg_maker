from copy import deepcopy

import pytest
import yaml

from pg_maker.assets import initialize_project
from pg_maker.builders.components import component_tap_skip_policy, verify_component_tests
from pg_maker.config import load_config
from pg_maker.errors import CommandError, PreconditionError, ValidationError
from pg_maker.storage import file_hash, write_json


def fixture(tmp_path):
    source = tmp_path / "source"
    (source / "t").mkdir(parents=True)
    script = source / "t/002_fixture.pl"
    script.write_text("plan skip_all => 'external fixture unavailable';\n")
    expected = {"regression": [], "isolation": [], "tap": ["t/001.pl", "t/002_fixture.pl"]}
    allowance = {
        "path": "t/002_fixture.pl",
        "source_sha256": file_hash(script),
        "skip_reason": "external fixture unavailable",
        "reason": "The original dataset host is unavailable.",
        "approval": "Operator approved this exact exception.",
    }
    policy = component_tap_skip_policy({"allowed_tap_skips": [allowance]}, source, expected)
    tap = tmp_path / "tap"
    tap.mkdir()
    passed = {
        "path": "t/001.pl",
        "tests": 3,
        "planned": 3,
        "failed": [],
        "skipped": [],
        "errors": [],
        "exit": 0,
        "wait": 0,
        "skip_all": "",
    }
    skipped = {
        **passed,
        "path": allowance["path"],
        "tests": 0,
        "planned": 0,
        "skip_all": allowance["skip_reason"],
    }
    write_json(tap / "passed.json", passed)
    write_json(tap / "skipped.json", skipped)
    return source, expected, policy, tap, skipped


def test_approved_skip_is_reported_but_never_counted_as_passed(tmp_path):
    _, expected, policy, tap, _ = fixture(tmp_path)
    evidence = verify_component_tests(expected, "", tap, allowed_skips=policy)
    assert evidence["tap_assertions"] == 3
    assert evidence["excluded_tap_suites"] == [
        {**policy["t/002_fixture.pl"], "status": "excluded", "counted_as_passed": False}
    ]
    with pytest.raises(CommandError, match="not explicitly allowed"):
        verify_component_tests(expected, "", tap)


@pytest.mark.parametrize(
    "change",
    [
        {"skip_all": "different reason"},
        {"exit": 1},
        {"wait": 9},
        {"errors": ["bad plan"]},
        {"failed": [1]},
        {"tests": 1},
        {"planned": 4},
        {"skipped": [1]},
        {"path": "t/unapproved.pl"},
    ],
)
def test_exception_never_hides_bad_or_unapproved_tap_results(tmp_path, change):
    _, expected, policy, tap, skipped = fixture(tmp_path)
    write_json(tap / "skipped.json", {**skipped, **change})
    with pytest.raises(CommandError):
        verify_component_tests(expected, "", tap, allowed_skips=policy)


def test_exception_does_not_hide_missing_test_result(tmp_path):
    _, expected, policy, tap, _ = fixture(tmp_path)
    (tap / "skipped.json").unlink()
    with pytest.raises(CommandError, match="inventory"):
        verify_component_tests(expected, "", tap, allowed_skips=policy)


def test_declared_exception_does_not_turn_executed_test_into_a_skip(tmp_path):
    _, expected, policy, tap, skipped = fixture(tmp_path)
    write_json(tap / "skipped.json", {**skipped, "skip_all": "", "tests": 4, "planned": 4})
    result = verify_component_tests(expected, "", tap, allowed_skips=policy)
    assert result["tap_assertions"] == 7 and result["excluded_tap_suites"] == []


def test_all_suites_excluded_cannot_qualify_a_component(tmp_path):
    _, expected, policy, tap, _ = fixture(tmp_path)
    (tap / "passed.json").unlink()
    expected["tap"].remove("t/001.pl")
    with pytest.raises(CommandError, match="no executed"):
        verify_component_tests(expected, "", tap, allowed_skips=policy)


@pytest.mark.parametrize("change", ["source", "missing", "inventory", "duplicate", "symlink"])
def test_exception_requires_unchanged_real_source_in_the_inventory(tmp_path, change):
    source, expected, policy, _, _ = fixture(tmp_path)
    entries = list(policy.values())
    script = source / entries[0]["path"]
    if change == "source":
        script.write_text("changed upstream test\n")
    elif change == "missing":
        script.unlink()
    elif change == "inventory":
        expected["tap"].remove(entries[0]["path"])
    elif change == "duplicate":
        entries.append(deepcopy(entries[0]))
    else:
        saved = script.with_suffix(".saved")
        script.rename(saved)
        script.symlink_to(saved.name)
    with pytest.raises(PreconditionError, match="source or inventory"):
        component_tap_skip_policy({"allowed_tap_skips": entries}, source, expected)


@pytest.mark.parametrize("change", ["duplicate", "glob", "traversal", "custom-command"])
def test_configuration_rejects_ambiguous_or_unenforced_exceptions(tmp_path, change):
    initialize_project(tmp_path, profile="yc-18.6-icu70")
    path = tmp_path / "components/managed18/rum.yaml"
    document = yaml.safe_load(path.read_text())
    spec = document["spec"]
    if change == "duplicate":
        spec["allowed_tap_skips"].append(deepcopy(spec["allowed_tap_skips"][0]))
    elif change in {"glob", "traversal"}:
        spec["allowed_tap_skips"][0]["path"] = "t/*.pl" if change == "glob" else "t/../other.pl"
    else:
        spec["test_commands"] = [{"id": "custom", "argv": ["true"], "success_pattern": "ok"}]
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValidationError, match="TAP skip exceptions"):
        load_config(tmp_path / "build.yaml")
