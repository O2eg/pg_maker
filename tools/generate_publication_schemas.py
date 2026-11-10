"""Generate publication configuration and durable request contracts."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/pg_maker/schemas"
S = {"type": "string", "minLength": 1, "pattern": r"^[^\u0000\r\n]+$"}
B = {"type": "boolean"}
P = {"type": "integer", "minimum": 1}
H = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def generate():
    auth = {
        "oneOf": [
            obj({"username_env": S, "password_env": S}),
            obj({"netrc_file": S}),
            obj({"bearer_env": S}),
        ]
    }
    repository = obj(
        {
            "id": S,
            "adapter": {"enum": ["nexus-apt", "nexus-yum", "nexus-raw", "directory"]},
            "formats": {
                "type": "array",
                "items": {"enum": ["deb", "rpm"]},
                "minItems": 1,
                "uniqueItems": True,
            },
            "url": S,
            "repository": S,
            "directory": S,
            "path_prefix": S,
            "auth": auth,
            "tls": obj({"verify": B, "ca_file": S}, []),
            "allow_http": B,
            "suite": S,
            "component": S,
        },
        ["id", "adapter", "formats"],
    )
    config = obj(
        {
            "api_version": {"const": "pg_maker/publication/v1"},
            "kind": {"const": "PublicationConfig"},
            "spec": obj(
                {
                    "repositories": {"type": "array", "items": repository, "minItems": 1},
                    "request_timeout_seconds": P,
                    "verification_timeout_seconds": P,
                    "poll_interval_seconds": P,
                    "source_timeout_seconds": P,
                },
                ["repositories"],
            ),
        }
    )
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pg-maker.dev/schemas/publication.schema.json",
        **config,
    }
    (ROOT / "publication.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
    package = obj(
        {
            "path": S,
            "filename": S,
            "format": {"enum": ["deb", "rpm"]},
            "sha256": H,
            "size_bytes": P,
            "name": S,
            "version": S,
            "architecture": S,
        }
    )
    destination = obj({"receiver": S, "package": package})
    receipt = obj(
        {
            "receiver": S,
            "package": package,
            "destination": S,
            "action": {"enum": ["uploaded", "reused"]},
            "index_verified": B,
        }
    )
    for name, definition in {
        "publication-plan": obj(
            {
                "api_version": {"const": "pg_maker/publication-plan/v1"},
                "source": S,
                "source_input_hash": H,
                "config_hash": H,
                "plan_hash": H,
                "receivers": {"type": "array", "items": repository, "minItems": 1},
                "destinations": {"type": "array", "items": destination, "minItems": 1},
            }
        ),
        "publication-result": obj(
            {
                "api_version": {"const": "pg_maker/publication-result/v1"},
                "source": S,
                "plan_hash": H,
                "status": {"enum": ["running", "incomplete", "succeeded"]},
                "qualification": {"enum": ["unverified", "publication-verified"]},
                "repository_installation_tests": {"const": "not-run"},
                "artifacts": {"type": "array", "items": receipt},
            }
        ),
    }.items():
        definition = {
            "$schema": schema["$schema"],
            "$id": "https://pg-maker.dev/schemas/" + name + ".schema.json",
            **definition,
        }
        (ROOT / (name + ".schema.json")).write_text(json.dumps(definition, indent=2) + "\n")
    branch = obj(
        {
            "api_version": {"const": "pg_maker/operation-input/v1"},
            "kind": {"const": "OperationInput"},
            "operation_kind": {"const": "publication"},
            "source": S,
            "source_input_hash": H,
            "config": obj(
                config["properties"]["spec"]["properties"], config["properties"]["spec"]["required"]
            ),
            "wait_for_source": B,
            "source_plan_hash": {"anyOf": [H, {"type": "null"}]},
        }
    )
    branch["required"].remove("source_plan_hash")
    p = ROOT / "operation-input.schema.json"
    data = json.loads(p.read_text())
    data["oneOf"] = [
        b
        for b in data["oneOf"]
        if b.get("properties", {}).get("operation_kind", {}).get("const") != "publication"
    ] + [branch]
    p.write_text(json.dumps(data, indent=2) + "\n")
    p = ROOT / "operation-state.schema.json"
    data = json.loads(p.read_text())
    kinds = data["properties"]["operation_kind"]["enum"]
    if "publication" not in kinds:
        kinds.append("publication")
    p.write_text(json.dumps(data, indent=2) + "\n")


if __name__ == "__main__":
    generate()
