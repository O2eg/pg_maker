"""Resolve documentation inputs without executing transformations or contacting providers."""

import copy
import re

from pg_maker.documents import validate_document
from pg_maker.errors import ValidationError
from pg_maker.postgresql import recipe
from pg_maker.sources.store import inventory
from pg_maker.storage import file_hash
from pg_maker.yamlio import load_yaml


def load_rules(references, base, prefix, bindings):
    documents = []
    for index, reference in enumerate(references):
        path = (base / reference).resolve() if isinstance(reference, str) else None
        document = load_yaml(path, "doc-rules") if path else copy.deepcopy(reference)
        validate_document(document, "doc-rules")
        directory = path.parent if path else base
        rules = []
        for rule in document["spec"]["rules"]:
            if rule["action"] == "copy-tree":
                source = (directory / rule["source"]).resolve()
                for entry in inventory(source)["entries"]:
                    if entry["type"] == "directory":
                        continue
                    if entry["type"] != "file":
                        raise ValidationError("documentation copy-tree accepts regular files only")
                    rules.append(
                        {
                            **rule,
                            "id": rule["id"] + "/" + entry["path"],
                            "action": "copy-file",
                            "file": rule["file"] + "/" + entry["path"],
                            "source": str(source / entry["path"]),
                        }
                    )
            else:
                rules.append(rule)
        for rule_index, rule in enumerate(rules):
            for name in ("fragment", "source"):
                if name not in rule:
                    continue
                if not isinstance(rule[name], str):
                    raise ValidationError("project documentation assets must name local files")
                asset = (directory / rule[name]).resolve()
                if not asset.is_file():
                    raise ValidationError("documentation asset does not exist")
                key = f"docs/{prefix}/{index}/{rule_index}/{name}"
                bindings[key] = str(asset)
                rule[name] = {"input": key, "sha256": file_hash(asset)}
        document["spec"]["rules"] = rules
        documents.append(document)
    return documents


def normalize(profile, base, bindings, *, version, validation):
    recipe(version)  # Refuse unknown majors before resolving any documentation inputs.
    if profile["install"] != {"html": "docs-package", "man": "owning-package"}:
        raise ValidationError("unsupported documentation installation policy")
    aliases = profile.get(
        "language_aliases", {"en": "en", "en-US": "en", "ru": "ru", "ru-RU": "ru"}
    )
    requested = list(profile["languages"])
    if not requested or any(language not in aliases for language in requested):
        raise ValidationError("unknown or empty documentation language selection")
    profile["languages"] = [aliases[language] for language in requested]
    if any(not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", lang) for lang in profile["languages"]):
        raise ValidationError("invalid normalized documentation language")
    if len(set(profile["languages"])) != len(requested):
        raise ValidationError("duplicate normalized documentation languages")
    profile["requested_languages"] = requested
    if profile["source_language"] != "en":
        raise ValidationError("postgres-tree has source language en")
    if not profile["formats"] or len(set(profile["formats"])) != len(profile["formats"]):
        raise ValidationError("documentation formats must be nonempty and unique")
    if validation["profile"] == "release":
        if profile["missing_translation"] != "error":
            raise ValidationError("release documentation forbids fallback")
        profile["require_reviewed"] = True
    profile["rules"] = load_rules(profile["rules"], base, "profile", bindings)
    pinned, seen = [], set()
    for index, reference in enumerate(profile["translations"]):
        if not isinstance(reference, str):
            raise ValidationError("project translation bundles must name local files")
        path = (base / reference).resolve()
        bundle = load_yaml(path, "translations")
        spec = bundle["spec"]
        language, feature = spec["target_language"], spec["context"].get("feature.id")
        target = spec["context"].get("target.id", "")
        if not feature or (feature, language, target) in seen:
            raise ValidationError("translation bundle requires a unique feature/language")
        seen.add((feature, language, target))
        key = "docs/translations/" + str(index)
        bindings[key] = str(path)
        pinned.append(
            {
                "input": key,
                "sha256": file_hash(path),
                "language": language,
                "feature": feature,
                "target": target,
            }
        )
    profile["translations"] = pinned


def references(config):
    docs = config["documentation"]
    if not docs["enabled"]:
        return []
    documents = list(docs["rules"])
    for feature in config["features"]:
        documents.extend(feature["spec"]["documentation"]["rules"])
        for component in feature["spec"]["components"]:
            documents.extend(component["spec"].get("documentation", {}).get("rules", []))
    return [
        *docs["translations"],
        *(
            rule[name]
            for document in documents
            for rule in document["spec"]["rules"]
            for name in ("fragment", "source")
            if name in rule
        ),
    ]
