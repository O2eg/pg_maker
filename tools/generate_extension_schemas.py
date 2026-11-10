"""Generate strict standalone-extension input schemas."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/pg_maker/schemas"
S = {"type": "string", "minLength": 1, "pattern": "^[^\\u0000\\r\\n]+$"}
P = {"type": "integer", "minimum": 1}
B = {"type": "boolean"}
HASH = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}


def obj(fields, required=None):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields) if required is None else required,
        "additionalProperties": False,
    }


def arr(items):
    return {"type": "array", "items": items}


def generate():
    target = obj(
        {
            "provider": {"enum": ["community", "profile", "custom"]},
            "major": {"type": "integer", "minimum": 14, "maximum": 99},
            "version": S,
            "profile": S,
            "os": obj({"id": {"enum": ["debian", "rocky"]}, "version": S}),
            "architecture": {"enum": ["amd64", "arm64"]},
            "format": {"enum": ["deb", "rpm"]},
            "base_image": S,
            "pg_config": S,
            "server_package": S,
            "sdk_packages": arr(S),
            "runtime_packages": arr(S),
            "packages": arr(obj({"path": S, "sha256": HASH})),
            "pgdg": B,
            "dependency_policy": {"enum": ["exact", "major"]},
            "split_bundled": B,
        },
        [
            "provider",
            "major",
            "os",
            "architecture",
            "format",
            "base_image",
            "pg_config",
            "server_package",
            "sdk_packages",
            "runtime_packages",
            "packages",
            "pgdg",
            "dependency_policy",
        ],
    )
    spec = obj(
        {
            "target": target,
            "extensions": {
                **arr(
                    obj(
                        {
                            "recipe": S,
                            "version": S,
                            "package_revision": P,
                            "package_name": S,
                            "license": S,
                        },
                        ["recipe", "version", "package_revision", "license"],
                    )
                ),
                "minItems": 1,
            },
            "execution": obj(
                {
                    "jobs": P,
                    "memory_bytes": P,
                    "min_free_disk_bytes": P,
                    "stage_timeout_seconds": P,
                    "keep_workspace": {"enum": ["always", "on-failure"]},
                }
            ),
            "validation": obj({"run_tests": B}),
            "maintainer": S,
        }
    )
    document = obj(
        {
            "api_version": {"const": "pg_maker/extension-build/v1"},
            "kind": {"const": "ExtensionBuild"},
            "spec": spec,
        }
    )
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:pg_maker:schema:extension-build:v1",
        **document,
    }
    (ROOT / "extension-build.schema.json").write_text(json.dumps(document, indent=2) + "\n")
    common = json.loads((ROOT / "planning-common.schema.json").read_text())["$defs"]
    locked_config = copy.deepcopy(spec)
    locked_target = locked_config["properties"]["target"]
    locked_target["properties"]["packages"] = arr(obj({"filename": S, "sha256": HASH}))
    component = copy.deepcopy(common["ComponentRecipe"])
    component["properties"]["packaging"] = copy.deepcopy(spec["properties"]["extensions"]["items"])
    packaging = component["properties"]["packaging"]
    packaging["properties"].pop("recipe")
    packaging["required"].remove("recipe")
    component["required"].append("packaging")
    properties = component["properties"]["spec"]["properties"]
    properties["source"] = common["ResolvedSource"]
    properties["source_unpatched"] = common["ResolvedSource"]
    properties["source_patches"] = arr(obj({"input": S, "sha256": HASH}))
    locked_config["properties"]["extensions"] = {**arr(component), "minItems": 1}
    sdk = obj(
        {
            "os": target["properties"]["os"],
            "layout": {"type": "object", "additionalProperties": {"type": "string"}},
            "postgresql_version": S,
            "server_package": obj({"name": S, "version": S, "architecture": S}),
            "sdk_files": arr(obj({"path": S, "sha256": HASH})),
            "sql_extensions": {"type": "object", "additionalProperties": {"type": "string"}},
        }
    )
    migration = arr(
        obj(
            {
                "name": S,
                "old_version": S,
                "version": S,
                "original_sha256": HASH,
                "migration_hash": HASH,
                "sha256": HASH,
                "filename": S,
                "removed": arr(obj({"path": S, "sha256": HASH, "extension": S})),
                "preserved_files": {"type": "integer", "minimum": 0},
                "preserved_payload_sha256": HASH,
            }
        )
    )
    locked = obj(
        {
            "config": locked_config,
            "target": sdk,
            "images": obj({"build": HASH, "test": HASH}),
            "base_image": HASH,
            "runtime_repository": arr(obj({"path": S, "sha256": HASH})),
            "migration": migration,
            "uid": P,
            "implementation": obj({"version": S, "code_hash": HASH}),
        }
    )
    locked["required"].remove("migration")
    planned = obj(
        {
            "lock_hash": HASH,
            "steps": arr(
                obj(
                    {
                        "id": S,
                        "kind": {
                            "enum": [
                                "verify-target",
                                "build",
                                "package",
                                "test-source",
                                "test-packages",
                                "manifest",
                            ]
                        },
                        "extension": S,
                    },
                    ["id", "kind"],
                )
            ),
        }
    )
    for kind, body in (("lock", locked), ("plan", planned)):
        generated = obj(
            {
                "api_version": {"const": "pg_maker/extension-" + kind + "/v1"},
                "kind": {"const": "Extension" + kind.title()},
                "spec": body,
                kind + "_hash": HASH,
                "created_at": S,
            }
        )
        if kind == "lock":
            generated["properties"]["transport"] = obj({"input_store": S})
        generated = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "urn:pg_maker:schema:extension-" + kind + ":v1",
            **generated,
        }
        (ROOT / ("extension-" + kind + ".schema.json")).write_text(
            json.dumps(generated, indent=2) + "\n"
        )
    path = ROOT / "operation-input.schema.json"
    operation = json.loads(path.read_text())
    branch = obj(
        {
            "api_version": {"const": "pg_maker/operation-input/v1"},
            "kind": {"const": "OperationInput"},
            "operation_kind": {"enum": ["extension_prepare", "extension_build"]},
            "config": {"type": "object"},
            "output": S,
            "input_store": S,
            "offline": B,
            "lock": {"type": ["object", "null"]},
        }
    )
    operation["oneOf"] = operation["oneOf"][:5] + [branch] + operation["oneOf"][6:]
    path.write_text(json.dumps(operation, indent=2) + "\n")
    path = ROOT / "operation-state.schema.json"
    state = json.loads(path.read_text())
    kinds = state["properties"]["operation_kind"]["enum"]
    for kind in ("extension_prepare", "extension_build"):
        if kind not in kinds:
            kinds.append(kind)
    path.write_text(json.dumps(state, indent=2) + "\n")


if __name__ == "__main__":
    generate()
