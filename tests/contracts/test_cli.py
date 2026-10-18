import json
import os
import subprocess
import sys
from importlib.resources import files

import pytest
from jsonschema import Draft202012Validator

from pg_maker.documents import schema, validate_document


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "pg_maker", *args], capture_output=True, text=True, timeout=10
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--machine", "nonsense"],
        ["nonsense", "--machine"],
        ["--machine", "--request-id"],
        ["runner", "check", "--machine"],
    ],
)
def test_argument_errors_have_single_machine_envelope(args):
    result = cli(*args)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    validate_document(payload, "component-envelope")
    assert payload["error"]["code"] == "validation_error"
    assert result.stderr == ""


def test_capabilities_advertise_only_implemented_commands():
    result = cli("--component-capabilities")
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert value["component"] == "pg_maker"
    assert "runner check" in value["commands"]
    assert value["commands"]["build"]["accepts_plan_hash"]
    assert "publish" not in value["commands"]
    assert value["commands"]["resume"]["accepts_plan_hash"]
    assert value["operation_kinds"]["build"]["resume"]
    assert not value["operation_kinds"]["container_commands"]["resume"]
    assert value["environment_checked"] is False


@pytest.mark.parametrize("command", ["plan", "build"])
def test_db_test_switches_are_explicit_and_mutually_exclusive(command):
    from pg_maker.cli import parser

    cli_parser = parser()
    assert cli_parser.parse_args([command, "lock.json"]).run_db_tests is None
    assert cli_parser.parse_args([command, "lock.json", "--run-db-tests"]).run_db_tests is True
    assert cli_parser.parse_args([command, "lock.json", "--skip-db-tests"]).run_db_tests is False
    result = cli(command, "lock.json", "--run-db-tests", "--skip-db-tests", "--machine")
    assert result.returncode == 2
    assert json.loads(result.stdout)["error"]["code"] == "validation_error"


def test_schemas_and_assets_in_installed_package():
    for name in ("operation-input", "operation-state", "run-event", "component-envelope"):
        Draft202012Validator.check_schema(schema(name))
    assert files("pg_maker").joinpath("bundled", "PROJECT.md").is_file()


def test_init_machine_and_module_entrypoint(tmp_path):
    result = cli("init", str(tmp_path / "project"), "--request-id", "init-1", "--machine")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    validate_document(payload, "component-envelope")
    assert payload["request_id"] == "init-1" and payload["result"]["created"]
    assert cli("--version").stdout.strip() == "pg-maker 0.1.0"


def test_init_documentation_selection_and_standalone_commands(tmp_path):
    directory = tmp_path / "project"
    result = cli("init", str(directory), "--with-doc", "--doc-language", "ru-RU", "--machine")
    assert result.returncode == 0, result.stdout
    result = cli("config", "show", str(directory / "build.yaml"), "--resolved", "--machine")
    assert result.returncode == 0, result.stdout
    documentation = json.loads(result.stdout)["result"]["data"]["documentation"]
    assert documentation["enabled"] is True
    assert documentation["languages"] == ["ru"]
    assert documentation["requested_languages"] == ["ru-RU"]
    for command in ("write", "prepare-translations", "apply-translations", "build"):
        result = cli("docs", command, "--help", "--machine")
        assert result.returncode == 0
        validate_document(json.loads(result.stdout), "component-envelope")


@pytest.mark.parametrize("args", [["--help"], ["runner", "check", "--help"], ["--version"]])
def test_help_and_version_preserve_machine_stdout(args):
    result = cli(*args, "--machine")
    assert result.returncode == 0
    validate_document(json.loads(result.stdout), "component-envelope")


def test_missing_docker_is_precondition_not_cleanup_failure(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pg_maker",
            "runner",
            "check",
            "--image",
            "sha256:" + "a" * 64,
            "--out",
            str(tmp_path),
            "--machine",
        ],
        env={**os.environ, "PATH": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)
    assert result.returncode == 3
    assert payload["error"]["code"] == "precondition_failed"
    assert payload["result"]["resources"] == []
