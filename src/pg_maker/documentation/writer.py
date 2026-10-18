"""Ordered, transactional edits of DocBook source files."""

from __future__ import annotations

import difflib
import html
import os
import re
import shutil
import tempfile
from pathlib import Path

from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, file_hash, read_json, write_json

from .xml import ATTR, NAME, parse, path_in, select, validate_book


def tree_hash(root: Path) -> str:
    value = inventory(root)
    value["entries"] = [e for e in value["entries"] if not e["path"].startswith(".pg-maker-")]
    return content_hash(value)


def variables(text: str, context: dict) -> str:
    def replace(match):
        if match[1] not in context or not isinstance(context[match[1]], str):
            raise ValidationError("unknown documentation variable", details={"variable": match[1]})
        return html.escape(context[match[1]], quote=True)

    return re.sub(r"\$\{([a-z][a-z0-9_.]*)\}", replace, text)


def atomic_tree(source, destination, operation, transform, *, receipt_name, check=lambda: None):
    source, destination = source.resolve(), destination.absolute()
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValidationError("documentation input/output trees must be disjoint")
    identity = content_hash({"input_hash": tree_hash(source), "operation": operation})
    if destination.exists():
        receipt = destination / receipt_name
        if receipt.is_file():
            previous = read_json(receipt)
            if previous["operation_hash"] == identity and previous["output_hash"] == tree_hash(
                destination
            ):
                return previous
        raise PreconditionError("documentation output already exists with another receipt")
    if (source / receipt_name).exists():
        raise PreconditionError("documentation transformation requires its clean input tree")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".doc-tree-") as directory:
        staged = Path(directory) / "tree"
        inventory(source)  # validate all links/special files before following any source path
        shutil.copytree(source, staged, symlinks=True)
        check()
        report = transform(staged)
        check()
        report.update(
            operation_hash=identity, input_hash=tree_hash(source), output_hash=tree_hash(staged)
        )
        diff = []
        paths = {
            p.relative_to(root).as_posix()
            for root in (source, staged)
            for p in root.rglob("*")
            if p.is_file() and not p.is_symlink() and p.suffix in {".sgml", ".xml", ".xsl"}
        }
        for name in sorted(paths):
            old, new = source / name, staged / name
            before = old.read_text() if old.exists() else ""
            after = new.read_text() if new.exists() else ""
            if before != after:
                diff.extend(
                    difflib.unified_diff(
                        before.splitlines(keepends=True),
                        after.splitlines(keepends=True),
                        fromfile="a/" + name,
                        tofile="b/" + name,
                    )
                )
        report["diff"] = "".join(diff)
        write_json(staged / receipt_name, report)
        os.replace(staged, destination)
    return report


def apply(
    source: Path,
    destination: Path,
    documents: list[dict],
    context: dict,
    *,
    assets: dict[str, Path],
    branding=True,
    source_language="en",
    check=lambda: None,
) -> dict:
    for document in documents:
        validate_document(document, "doc-rules")

    def transform(root):
        results, localized, seen = [], [], set()
        if branding:
            title = "${vendor.brand} ${product.version} — ${feature.id} Documentation"
            brand_rules = [
                {
                    "id": "brand-title",
                    "action": "set-text",
                    "file": "postgres.sgml",
                    "selector": {"tag": "title", "parent": {"id": "postgres"}},
                    "expect_matches": 1,
                    "text": title,
                },
                {
                    "id": "brand-product",
                    "action": "set-text",
                    "file": "postgres.sgml",
                    "selector": {"tag": "productname", "parent": {"tag": "bookinfo"}},
                    "expect_matches": 1,
                    "text": "${vendor.product_name}",
                },
                {
                    "id": "brand-version",
                    "action": "set-text",
                    "file": "postgres.sgml",
                    "selector": {"tag": "productnumber", "parent": {"tag": "bookinfo"}},
                    "expect_matches": 1,
                    "text": "${product.version}",
                },
            ]
        else:
            brand_rules = []
        rules = [
            {
                "id": "source-language",
                "action": "set-attribute",
                "file": "postgres.sgml",
                "selector": {"tag": "book"},
                "expect_matches": 1,
                "attribute": "lang",
                "text": source_language,
            },
            *brand_rules,
            *[r for doc in documents for r in doc["spec"]["rules"]],
        ]
        for rule in rules:
            check()
            if rule["id"] in seen:
                raise ValidationError("duplicate documentation rule ID", details={"id": rule["id"]})
            seen.add(rule["id"])
            if rule.get("features") and context["feature.id"] not in rule["features"]:
                results.append({"id": rule["id"], "status": "not-applicable"})
                continue
            path = path_in(root, rule["file"])
            before = file_hash(path) if path.is_file() else None
            action = rule["action"]

            def fragment(key="fragment", rule=rule):
                ref = rule[key]
                if not isinstance(ref, dict) or ref["input"] not in assets:
                    raise ValidationError("documentation asset must be fixed before execution")
                asset = assets[ref["input"]]
                if file_hash(asset) != ref["sha256"]:
                    raise PreconditionError("documentation asset hash changed")
                return asset.read_bytes()

            if action in {"copy-file", "replace-file", "add-page", "release-notes"}:
                count = int(path.exists())
                if count != rule["expect_matches"] or (action != "replace-file" and count):
                    raise PreconditionError(
                        "documentation file cardinality mismatch",
                        details={"id": rule["id"], "matches": count},
                    )
                data = fragment("source" if action in {"copy-file", "replace-file"} else "fragment")
                if path.suffix in {".sgml", ".xml"}:
                    data = variables(data.decode(), context).encode()
                    parse(data.decode())
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(0o644)
                if action in {"add-page", "release-notes"}:
                    entity = rule["entity"]
                    if not re.fullmatch(NAME, entity):
                        raise ValidationError("invalid page entity")
                    include = path_in(root, rule["include_file"])
                    text = include.read_text()
                    found = select(parse(text), rule["selector"])
                    if len(found) != 1:
                        raise PreconditionError("page include selector must match exactly once")
                    index = found[0].close_start
                    include.write_text(text[:index] + "\n &" + entity + ";\n" + text[index:])
                    declarations = root / "filelist.sgml"
                    if not declarations.is_file():
                        raise PreconditionError("page addition requires filelist.sgml")
                    declarations.write_text(
                        declarations.read_text()
                        + "\n<!ENTITY "
                        + entity
                        + ' SYSTEM "'
                        + html.escape(rule["file"], quote=True)
                        + '">\n'
                    )
                if rule.get("translate") is False:
                    if not rule.get("language"):
                        raise ValidationError("localized page needs an explicit language")
                    localized.append({"path": rule["file"], "language": rule["language"]})
            else:
                if not path.is_file():
                    raise PreconditionError(
                        "documentation rule file is missing", details={"id": rule["id"]}
                    )
                text = path.read_text()
                if action == "exact-text":
                    if before != rule.get("sha256") or not rule.get("old"):
                        raise PreconditionError(
                            "exact text edit requires matching file hash and nonempty old text"
                        )
                    count = text.count(rule["old"])
                    if count != rule["expect_matches"]:
                        raise PreconditionError("exact text cardinality mismatch")
                    text = text.replace(rule["old"], variables(rule["text"], context))
                else:
                    found = select(parse(text), rule["selector"])
                    count = len(found)
                    if count != rule["expect_matches"]:
                        raise PreconditionError(
                            "documentation selector cardinality mismatch",
                            details={"id": rule["id"], "matches": count},
                        )
                    if any(a.start < b.start < a.end for a in found for b in found):
                        raise ValidationError("documentation selector matches overlapping nodes")
                    for node in reversed(found):
                        if action in {
                            "insert-before",
                            "insert-after",
                            "append",
                            "prepend",
                            "replace",
                        }:
                            value = variables(fragment().decode(), context)
                            parse(value)
                            start, end = {
                                "insert-before": (node.start, node.start),
                                "insert-after": (node.end, node.end),
                                "append": (node.close_start, node.close_start),
                                "prepend": (node.open_end, node.open_end),
                                "replace": (node.start, node.end),
                            }[action]
                        elif action == "remove":
                            start, end, value = node.start, node.end, ""
                        elif action == "set-text":
                            if node.children or node.open_end == node.end:
                                raise ValidationError(
                                    "set-text requires a text element without child elements"
                                )
                            start, end, value = (
                                node.open_end,
                                node.close_start,
                                variables(html.escape(rule["text"], quote=True), context),
                            )
                        elif action == "set-attribute":
                            attribute = rule["attribute"]
                            if not re.fullmatch(NAME, attribute) or attribute.startswith("xmlns"):
                                raise ValidationError("invalid documentation attribute")
                            value = variables(html.escape(rule["text"], quote=True), context)
                            start, end = node.start, node.open_end
                            opening = text[start:end]
                            existing = next(
                                (
                                    m
                                    for m in ATTR.finditer(opening)
                                    if m[1].split("=")[0].strip() == attribute
                                ),
                                None,
                            )
                            if existing:
                                value = (
                                    opening[: existing.start(3)]
                                    + value
                                    + opening[existing.end(3) :]
                                )
                            else:
                                stop = -2 if opening.endswith("/>") else -1
                                value = opening[:stop] + f' {attribute}="{value}"' + opening[stop:]
                        else:
                            raise ValidationError("unsupported documentation action")
                        text = text[:start] + value + text[end:]
                parse(text)
                path.write_text(text)
            results.append(
                {
                    "id": rule["id"],
                    "path": rule["file"],
                    "matches": count,
                    "status": "passed",
                    "input_hash": before,
                    "output_hash": file_hash(path),
                }
            )
        validation = validate_book(root)
        return {
            "api_version": "pg_maker/doc-writer-result/v1",
            "rules": results,
            "localized": localized,
            "validation": validation,
            "warnings": [],
        }

    return atomic_tree(
        source,
        destination,
        {
            "writer": "v1",
            "rules": documents,
            "context": context,
            "branding": branding,
            "source_language": source_language,
        },
        transform,
        receipt_name=".pg-maker-writer.json",
        check=check,
    )
