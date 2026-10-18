"""Validate change/component recipes and their ordered, explicit file inputs."""

import re
from pathlib import Path

from pg_maker.documents import relative_output
from pg_maker.errors import ValidationError
from pg_maker.planning.graph import ordered_ids
from pg_maker.storage import content_hash, file_hash
from pg_maker.yamlio import load_yaml


def _ordered(documents: list[dict]) -> list[dict]:
    indexed = {}
    for document in documents:
        identifier = document["metadata"]["id"]
        if identifier in indexed and indexed[identifier] != document:
            raise ValidationError(
                "conflicting recipe revisions or content", details={"id": identifier}
            )
        indexed.setdefault(identifier, document)
    for identifier, document in indexed.items():
        conflict = set(document["spec"]["conflicts"]) & indexed.keys()
        if conflict:
            raise ValidationError(
                "conflicting recipes", details={"id": identifier, "conflicts": sorted(conflict)}
            )
    ids = ordered_ids(
        [{"id": key, "requires": doc["spec"]["requires"]} for key, doc in indexed.items()]
    )
    return [indexed[key] for key in ids]


def load_changes(references: list[str], base: Path, version: str) -> tuple[list[dict], dict]:
    from pg_maker.config import compatible

    documents, bindings = [], {}
    for reference in references:
        path = (base / reference).resolve()
        document = load_yaml(path, "change-set")
        identifier, spec = document["metadata"]["id"], document["spec"]
        compatible(version, spec["compatible_postgresql"], identifier)
        operations = set()
        for operation in spec["operations"]:
            if operation["id"] in operations:
                raise ValidationError("duplicate change operation id")
            operations.add(operation["id"])
            source = (path.parent / operation["file"]).resolve()
            if operation["kind"] == "insert_text":
                insertion = load_yaml(source, "insertions")
                if not relative_output(insertion["target_file"]):
                    raise ValidationError("insertion target must be a relative source path")
                seen = set()
                for edit in insertion["modifications"]:
                    if edit["id"] in seen or any(
                        "\n" in edit[key] or "\r" in edit[key] for key in ("top", "bot")
                    ):
                        raise ValidationError("duplicate insertion id or multiline anchor")
                    seen.add(edit["id"])
                    if not edit["top"] or not edit["bot"]:
                        raise ValidationError("insertion anchors must be nonempty")
            key = "changes/" + identifier + "/" + operation["id"]
            if not source.is_file():
                raise ValidationError("change input is missing", details={"input": key})
            operation["file"] = {"input": key, "sha256": file_hash(source)}
            bindings[key] = str(source)
        tests = set()
        for test in spec["acceptance"]:
            if test["id"] in tests:
                raise ValidationError("duplicate acceptance id")
            tests.add(test["id"])
            assertion_ids = set()
            for assertion in test["assertions"]:
                if (
                    assertion["id"] in assertion_ids
                    or len(set(assertion) & {"contains", "equals"}) != 1
                ):
                    raise ValidationError("acceptance needs unique assertions with one comparison")
                assertion_ids.add(assertion["id"])
            for expectation in test["expectations"].values():
                if (
                    expectation["outcome"] == "assertion-failure"
                    and expectation.get("assertion_id") not in assertion_ids
                ):
                    raise ValidationError("expected failure must name an existing assertion")
            source = (path.parent / test["script"]).resolve()
            if not source.is_file():
                raise ValidationError("acceptance script is missing")
            key = "changes/" + identifier + "/acceptance/" + test["id"]
            test["script"] = {"input": key, "sha256": file_hash(source)}
            bindings[key] = str(source)
        documents.append(document)
    return _ordered(documents), bindings


def load_components(references: list[str], base: Path, version: str) -> tuple[list[dict], dict]:
    from pg_maker.config import compatible

    documents, bindings = [], {}
    for reference in references:
        path = (base / reference).resolve()
        document = load_yaml(path, "component")
        identifier, spec = document["metadata"]["id"], document["spec"]
        compatible(version, spec["compatible_postgresql"], identifier)
        if spec["type"] != "contrib" and "source" not in spec:
            raise ValidationError("external component needs a source")
        if spec["type"] == "contrib" and "source" in spec:
            raise ValidationError("contrib must use the PostgreSQL source")
        if spec.get("source_directory") and not relative_output(spec["source_directory"]):
            raise ValidationError("component directory must be relative")
        for key in ("make_targets", "test_targets", "install_targets"):
            if any(
                not re.fullmatch(r"[a-zA-Z0-9_./-]+", x) or x.startswith("-") for x in spec[key]
            ):
                raise ValidationError("invalid component make target")
        bindings["components/" + identifier] = str(path.parent)
        documents.append(document)
    # Duplicate references are harmless only when their parsed content is identical.
    return _ordered(documents), bindings


def required_tests(feature: dict, branch: str = "candidate") -> list[str]:
    result = list(feature["spec"]["required_tests"]) if branch == "candidate" else []
    acceptance = {}
    for change in feature["spec"]["changes"]:
        for test in change["spec"]["acceptance"]:
            if branch not in test["expectations"]:
                continue
            if test["id"] in acceptance and acceptance[test["id"]] != content_hash(test):
                raise ValidationError("different acceptance tests use the same id")
            acceptance[test["id"]] = content_hash(test)
            result.append(test["id"])
    for component in feature["spec"]["components"]:
        result.extend(component["spec"]["required_tests"])
    return list(dict.fromkeys(result))
