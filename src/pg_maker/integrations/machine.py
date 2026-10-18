"""pg_play/component/v1 without a runtime dependency on pg_play."""

from typing import Any

from pg_maker import __version__
from pg_maker.security import redact

EXIT_CODES = {
    "success": 0,
    "validation_error": 2,
    "precondition_failed": 3,
    "unsupported": 4,
    "partial": 5,
    "execution_error": 6,
    "cancelled": 7,
    "ownership_error": 8,
}
COMMANDS = {
    "dev open": True,
    "dev status": False,
    "dev patch": True,
    "dev build": True,
    "dev explain": True,
    "dev exec": True,
    "dev pin": True,
    "dev prune": True,
    "init": True,
    "doctor": False,
    "runner check": True,
    "status": False,
    "events": False,
    "artifacts": False,
    "cancel": True,
    "wait": False,
    "config show": False,
    "features": False,
    "package names": False,
    "validate": False,
    "platforms": False,
    "env prepare": True,
    "resolve": True,
    "plan": False,
    "build": True,
    "resume": True,
    "export": True,
    "runtime plan": False,
    "runtime build": True,
    "cache prune": True,
    "cleanup": True,
    "changes workspace": True,
    "changes validate": False,
    "changes export": True,
    "docs build": True,
    "docs write": True,
    "docs prepare-translations": True,
    "docs apply-translations": True,
}


def capabilities() -> dict[str, Any]:
    return {
        "capability_schema_version": "pg_play/capabilities/v1",
        "contract_version": "pg_play/component/v1",
        "component": "pg_maker",
        "component_version": __version__,
        "commands": {
            name: {
                "mutates_target": mutates,
                "machine_output": True,
                "accepts_plan_hash": name in {"build", "resume", "runtime build"}
                or name.startswith("docs "),
            }
            for name, mutates in COMMANDS.items()
        },
        "exit_codes": EXIT_CODES,
        "machine_interface": {
            "machine_flag": "--machine",
            "request_id_option": "--request-id",
            "capabilities_option": "--component-capabilities",
        },
        "secret_policy": {
            "input": "environment references",
            "persist_values": False,
            "command_output": "streaming literal redaction",
        },
        "document_schemas": [
            "pg_maker/operation-input/v1",
            "pg_maker/operation/v1",
            "pg_maker/run-event/v1",
            "pg_maker/v1",
            "pg_maker/environment/v1",
            "pg_maker/lock/v1",
            "pg_maker/plan/v1",
            "pg_maker/doc-rules/v1",
            "pg_maker/translations/v1",
        ],
        "artifact_roles": [
            "command_log",
            "operation_output",
            "server",
            "client",
            "libpq",
            "baseline-package",
            "docs",
            "documentation",
        ],
        "operation_kinds": {
            key: {"resume": key not in {"container_commands", "development_command"}}
            for key in (
                "container_commands",
                "prepare_environments",
                "resolve_inputs",
                "build",
                "runtime_image",
                "development_command",
            )
        },
        "environment_checked": False,
    }


def envelope(
    command: str,
    request_id: str | None,
    *,
    result: Any = None,
    error: dict | None = None,
    status: str = "succeeded",
) -> dict[str, Any]:
    if status == "interrupted":
        status = "failed"
    return {
        "contract_version": "pg_play/component/v1",
        "component": "pg_maker",
        "component_version": __version__,
        "command": command,
        "request_id": request_id,
        "status": status,
        # Capabilities describe secret handling; the secret_policy field contains
        # public metadata, not a credential value.
        "result": result if command == "capabilities" else redact(result),
        "artifacts": _artifact_references(result),
        "warnings": [],
        "error": redact(error),
    }


def _artifact_references(result: Any) -> list[dict]:
    if not isinstance(result, dict):
        return []
    if isinstance(result.get("artifacts"), list):
        return redact(result["artifacts"])
    output = result.get("result") or result
    if not isinstance(output, dict):
        return []
    if isinstance(output.get("artifacts"), list):
        return redact(output["artifacts"])
    refs = [{**r, "role": "report"} for r in output.get("reports", [])]
    if "document" in output and "sha256" in output:
        refs.append(
            {"path": output["document"], "sha256": output["sha256"], "role": "operation_output"}
        )
    return redact(refs)
