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
        if spec.get("source_patches") and "source" not in spec:
            raise ValidationError("component source patches require an external source")
        patches = []
        for index, reference in enumerate(spec.get("source_patches", [])):
            source = (path.parent / reference).resolve()
            if not source.is_file():
                raise ValidationError("component source patch is missing")
            key = "components/" + identifier + "/source-patches/" + str(index)
            patches.append({"input": key, "sha256": file_hash(source)})
            bindings[key] = str(source)
        if patches:
            spec["source_patches"] = patches
        if spec.get("extra_sources") and spec["type"] in {"contrib", "source-tree"}:
            raise ValidationError("extra_sources require an external build directory")
        for name in spec.get("extra_sources", {}):
            if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                raise ValidationError("invalid extra source name")
        if spec.get("source_directory") and not relative_output(spec["source_directory"]):
            raise ValidationError("component directory must be relative")
        if spec.get("build_system") == "cmake" and spec["type"] not in {"utility", "library"}:
            raise ValidationError("CMake components must have type utility or library")
        for name, expected in spec.get("sql_extension_versions", {}).items():
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or not expected:
                raise ValidationError("invalid extension version assertion")
        for key in ("make_targets", "test_targets", "install_targets"):
            if any(
                not re.fullmatch(r"[a-zA-Z0-9_./-]+", x) or x.startswith("-") for x in spec[key]
            ):
                raise ValidationError("invalid component make target")
        test_ids = set()
        for test in spec.get("test_commands", []):
            if test["id"] in test_ids:
                raise ValidationError("duplicate native test id")
            test_ids.add(test["id"])
            try:
                pattern = re.compile(test["success_pattern"])
            except re.error as exc:
                raise ValidationError("invalid native test success pattern") from exc
            if pattern.search(""):
                raise ValidationError("native test success pattern must not match empty output")
        bindings["components/" + identifier] = str(path.parent)
        documents.append(document)
    # Library dependencies must exist before PostgreSQL's configure, not after its install.
    ordered = _ordered(documents)
    early = {
        c["metadata"]["id"] for c in ordered if c["spec"].get("build_phase") == "before-postgresql"
    }
    provided = set()
    for component in ordered:
        spec = component["spec"]
        if component["metadata"]["id"] in early:
            if spec["type"] != "library" or set(spec["requires"]) - early:
                raise ValidationError(
                    "before-postgresql libraries cannot depend on PostgreSQL components"
                )
            if not spec.get("pkg_config_versions"):
                raise ValidationError("before-postgresql libraries need exact pkg_config_versions")
        elif spec.get("provides_options") or spec.get("pkg_config_versions"):
            raise ValidationError("library version policy requires before-postgresql phase")
        options = set(spec.get("provides_options", []))
        if options & provided:
            raise ValidationError("multiple libraries provide the same PostgreSQL option")
        provided.update(options)
        for module, version in spec.get("pkg_config_versions", {}).items():
            if not re.fullmatch(r"[A-Za-z0-9_+.-]+", module) or not re.fullmatch(
                r"[0-9][A-Za-z0-9_.+-]*", version
            ):
                raise ValidationError("invalid pkg-config module or exact version")
    return ordered, bindings


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
