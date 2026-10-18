"""Installed JSON schemas and semantic validation of implemented documents."""

from __future__ import annotations

import re
from functools import cache
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from pg_maker.errors import ValidationError
from pg_maker.security import sensitive_name
from pg_maker.storage import parse_json


@cache
def schema(name: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise ValidationError(f"unknown schema: {name}")
    path = files("pg_maker").joinpath("schemas", f"{name}.schema.json")
    if not path.is_file():
        raise ValidationError(f"unknown schema: {name}")
    return parse_json(path.read_text(encoding="utf-8"))


def _no_floats(value: Any, pointer: str = "") -> None:
    if isinstance(value, float):
        raise ValidationError("use integer units or decimal strings", details={"pointer": pointer})
    if isinstance(value, dict):
        for key, item in value.items():
            _no_floats(item, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_floats(item, f"{pointer}/{index}")


def relative_output(path: str) -> bool:
    parts = PurePosixPath(path)
    return (
        bool(path)
        and not parts.is_absolute()
        and ".." not in parts.parts
        and str(parts) == path
        and path != "."
        and "\x00" not in path
    )


def validate_document(value: Any, name: str) -> None:
    selected = schema(name)
    if name == "operation-input" and isinstance(value, dict) and "oneOf" in selected:
        # Select the discriminated branch before validation to keep precise field pointers.
        selected = selected["oneOf"][
            {"container_commands": 0, "build": 2, "runtime_image": 3, "development_command": 4}.get(
                value.get("operation_kind"), 1
            )
        ]
    errors = sorted(
        Draft202012Validator(selected, registry=schema_registry()).iter_errors(value),
        key=lambda e: str(e.path),
    )
    if errors:
        error = errors[0]
        path = list(error.absolute_path)
        if error.validator == "additionalProperties" and isinstance(error.instance, dict):
            extra = sorted(set(error.instance) - set(error.schema.get("properties", {})))
            if extra:
                path.append(extra[0])
        pointer = "".join("/" + str(x).replace("~", "~0").replace("/", "~1") for x in path)
        # jsonschema messages may contain the entire invalid value (including credentials).
        raise ValidationError(f"document violates {error.validator}", details={"pointer": pointer})
    _no_floats(value)
    if name != "operation-input" or value["operation_kind"] != "container_commands":
        return
    ids: set[str] = set()
    for index, command in enumerate(value["commands"]):
        pointer = f"/commands/{index}"
        if command["id"] in ids:
            raise ValidationError("duplicate command id", details={"pointer": pointer + "/id"})
        ids.add(command["id"])
        if any("\x00" in part for part in command["argv"]):
            raise ValidationError("argv contains NUL", details={"pointer": pointer + "/argv"})
        if any(sensitive_name(key) for key in command["env"]):
            raise ValidationError("use secret_env for credentials", details={"pointer": pointer})
        if set(command["env"]) & set(command["secret_env"]):
            raise ValidationError("env and secret_env overlap", details={"pointer": pointer})
        if any("\n" in val or "\r" in val for val in command["env"].values()):
            raise ValidationError(
                "env values must be single-line strings", details={"pointer": pointer}
            )
        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source)
            for source in command["secret_env"].values()
        ):
            raise ValidationError(
                "invalid secret environment reference", details={"pointer": pointer}
            )
        if not re.fullmatch(r"/work/(source|build|output)(/[^\x00]*)?", command["cwd"]):
            raise ValidationError(
                "cwd must use a /work path", details={"pointer": pointer + "/cwd"}
            )
        if ".." in PurePosixPath(command["cwd"]).parts:
            raise ValidationError("cwd must not traverse parents", details={"pointer": pointer})
    if any(not relative_output(path) for path in value["outputs"]):
        raise ValidationError("outputs must be normalized relative paths")


@cache
def schema_registry() -> Registry:
    registry = Registry()
    for path in files("pg_maker").joinpath("schemas").iterdir():
        if path.name.endswith(".schema.json"):
            value = parse_json(path.read_text(encoding="utf-8"))
            registry = registry.with_resource(value["$id"], Resource.from_contents(value))
    return registry
