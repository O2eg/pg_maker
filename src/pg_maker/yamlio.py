"""JSON-compatible YAML with explicit duplicate-key, alias and tag rejection."""

from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent

from pg_maker.documents import validate_document
from pg_maker.errors import ValidationError
from pg_maker.storage import canonical_json


class StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(AliasEvent):
            raise ValidationError("YAML aliases are not supported")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise ValidationError("YAML mapping keys must be strings")
            if key in result:
                raise ValidationError(
                    "duplicate YAML key", details={"key": key, "line": key_node.start_mark.line + 1}
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def load_yaml(path: Path, schema_name: str | None = None) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=StrictLoader)
    except OSError as exc:
        raise ValidationError("cannot read YAML document", details={"path": str(path)}) from exc
    except yaml.YAMLError as exc:
        # PyYAML's exception text can echo secrets from the offending source line.
        mark = getattr(exc, "problem_mark", None)
        raise ValidationError(
            "invalid or unsupported YAML",
            details={
                "path": str(path),
                "line": mark.line + 1 if mark else None,
            },
        ) from exc
    if not isinstance(value, dict):
        raise ValidationError("YAML document must be a mapping")
    canonical_json(value)
    if schema_name:
        validate_document(value, schema_name)
    return value
