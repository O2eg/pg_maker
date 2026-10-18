"""Offline segment extraction, import validation and deterministic bundle application."""

from __future__ import annotations

import html
import re
from collections import Counter
from pathlib import Path

from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.storage import content_hash, read_json

from .writer import atomic_tree, tree_hash
from .xml import ENTITY, markup, parse, validate_book

BLOCKS = {"title", "subtitle", "para", "simpara", "entry", "term", "refpurpose", "remark"}
PROTECTED = {
    "programlisting",
    "screen",
    "synopsis",
    "literallayout",
    "literal",
    "command",
    "function",
    "parameter",
    "varname",
    "filename",
    "option",
    "type",
    "structname",
    "structfield",
    "token",
    "computeroutput",
    "userinput",
    "productname",
    "productnumber",
}
PLACEHOLDER = re.compile(r"\{\{p[0-9]{4,}\}\}")


def segments(root: Path, target_language: str, glossary: dict, context: dict) -> list[dict]:
    localized = {}
    receipt = root / ".pg-maker-writer.json"
    if receipt.exists():
        localized = {item["path"]: item["language"] for item in read_json(receipt)["localized"]}
    result = []
    for path in sorted(root.rglob("*.sgml")):
        relative = path.relative_to(root).as_posix()
        if relative in localized:
            if localized[relative] != target_language:
                raise PreconditionError(
                    "localized fragment does not match selected language",
                    details={"path": relative, "language": target_language},
                )
            continue
        text, ordinal = path.read_text(), Counter()
        nodes = parse(text)
        for node in nodes:
            if node.tag not in BLOCKS or any(c.tag in BLOCKS for c in descendants(node)):
                continue
            ancestor, protected, identifier = node, False, "root"
            while ancestor:
                protected |= ancestor.tag in PROTECTED or ancestor.tag in {
                    "copyright",
                    "legalnotice",
                }
                if identifier == "root" and ancestor.attrs.get("id"):
                    identifier = ancestor.attrs["id"]
                ancestor = ancestor.parent
            if protected or node.open_end == node.end:
                continue
            key = identifier + "/" + node.tag
            ordinal[key] += 1
            location = key + "/" + str(ordinal[key])
            raw = text[node.open_end : node.close_start]
            spans = [
                (n.start - node.open_end, n.end - node.open_end)
                for n in descendants(node)
                if n.tag in PROTECTED
            ]
            # Nested protected nodes remain one exact byte placeholder.
            spans = [
                span
                for span in spans
                if not any(
                    other != span and other[0] <= span[0] and span[1] <= other[1] for other in spans
                )
            ]
            spans += [
                (a, b) for a, b, _ in markup(raw) if not any(a >= x and b <= y for x, y in spans)
            ]
            spans += [
                m.span()
                for m in ENTITY.finditer(raw)
                if not any(m.start() >= x and m.end() <= y for x, y in spans)
            ]
            values = {
                context[key]
                for key in ("vendor.brand", "vendor.product_name", "product.version", "feature.id")
                if context.get(key)
            }
            for value in sorted(values, key=len, reverse=True):
                for match in re.finditer(r"(?<!\w)" + re.escape(value) + r"(?!\w)", raw):
                    if not any(match.start() < end and start < match.end() for start, end in spans):
                        spans.append(match.span())
            placeholders, chunks, start = {}, [], 0
            for a, b in sorted(spans):
                token = "{{p" + str(len(placeholders)).zfill(4) + "}}"
                chunks.extend([html.unescape(raw[start:a]), token])
                placeholders[token] = raw[a:b]
                start = b
            chunks.append(html.unescape(raw[start:]))
            source = "".join(chunks)
            if not any(c.isalpha() for c in PLACEHOLDER.sub("", source)):
                continue
            source_hash = content_hash(
                {
                    "text": source,
                    "placeholders": placeholders,
                    "context": location,
                    "glossary": glossary,
                    "product": context,
                }
            )
            result.append(
                {
                    "id": content_hash({"path": relative, "context": location}),
                    "path": relative,
                    "context": location,
                    "source": source,
                    "source_hash": source_hash,
                    "placeholders": placeholders,
                    "translation": None,
                    "state": "missing",
                    "provenance": {},
                    "_start": node.open_end,
                    "_end": node.close_start,
                }
            )
    return result


def descendants(node):
    for child in node.children:
        yield child
        yield from descendants(child)


def extract(
    root: Path,
    *,
    source_language: str,
    target_language: str,
    context: dict,
    glossary: dict | None = None,
) -> dict:
    glossary = glossary or {"revision": "1", "terms": {}}
    validate_book(root)
    document = {
        "api_version": "pg_maker/translations/v1",
        "kind": "TranslationBundle",
        "metadata": {"id": "documentation", "revision": 1},
        "spec": {
            "source_tree_hash": tree_hash(root),
            "source_language": source_language,
            "target_language": target_language,
            "context": context,
            "glossary": glossary,
            "segments": [
                {k: v for k, v in segment.items() if not k.startswith("_")}
                for segment in segments(root, target_language, glossary, context)
            ],
        },
    }
    validate_document(document, "translations")
    return document


def quality(
    root: Path,
    bundle: dict,
    *,
    source_language: str,
    target_language: str,
    context: dict,
    glossary: dict,
    require_reviewed=False,
    fallback=False,
) -> tuple[dict, list]:
    validate_document(bundle, "translations")
    spec = bundle["spec"]
    if any(
        spec[key] != value
        for key, value in {
            "source_language": source_language,
            "target_language": target_language,
            "context": context,
            "glossary": glossary,
        }.items()
    ):
        raise PreconditionError(
            "translation bundle context/language/glossary differs from the requested target"
        )
    expected = segments(root, target_language, glossary, context)
    supplied = {item["id"]: item for item in spec["segments"]}
    if len(supplied) != len(spec["segments"]) or set(supplied) - {item["id"] for item in expected}:
        raise ValidationError("translation bundle has duplicate or unknown segments")
    counts, failures, pages, replacements = Counter(), [], {}, []
    for original in expected:
        item = supplied.get(original["id"])
        state = item["state"] if item else "missing"
        if item and any(
            item[key] != original[key]
            for key in ("path", "context", "source", "source_hash", "placeholders")
        ):
            state = "stale"
        if state in {"translated", "reviewed"}:
            translated = item["translation"]
            if not isinstance(translated, str) or not translated.strip() or not item["provenance"]:
                raise ValidationError("successful translation requires text and provenance")
            if PLACEHOLDER.findall(translated) != PLACEHOLDER.findall(original["source"]):
                raise ValidationError(
                    "translation changed or reordered protected placeholders",
                    details={"id": item["id"]},
                )
            if "{{p" in PLACEHOLDER.sub("", translated):
                raise ValidationError("translation contains an invalid placeholder")
            if translated == original["source"] and state != "reviewed":
                raise ValidationError(
                    "unreviewed source copy is not a translation", details={"id": item["id"]}
                )
            for term, target in glossary["terms"].items():
                if term in original["source"] and target not in translated:
                    raise ValidationError(
                        "translation glossary violation", details={"id": item["id"], "term": term}
                    )
            if require_reviewed and state != "reviewed":
                failures.append({"id": original["id"], "state": "not-reviewed"})
            value = html.escape(translated, quote=False)
            for token, raw in original["placeholders"].items():
                value = value.replace(token, raw)
            replacements.append({**original, "value": value})
        else:
            failures.append({"id": original["id"], "state": state})
        counts[state] += 1
        pages.setdefault(original["path"], Counter())[state] += 1
    stale_tree = spec["source_tree_hash"] != tree_hash(root)
    if stale_tree:
        failures.append({"id": "source-tree", "state": "stale"})
    report = {
        "complete": not failures,
        "counts": dict(counts),
        "pages": {k: dict(v) for k, v in pages.items()},
        "failures": failures,
        "fallback": bool(failures and fallback),
        "semantic_accuracy": "not-proven",
        "reviewed": counts["reviewed"],
        "bundle_hash": content_hash(bundle),
    }
    # Fallback cannot bypass a changed source tree or a reviewed-only policy.
    if failures and (not fallback or stale_tree or require_reviewed):
        raise PreconditionError(
            "translation bundle is incomplete or stale; run docs prepare-translations "
            "and import a matching bundle",
            details={"quality": report},
        )
    return report, replacements


def apply(
    root: Path,
    destination: Path,
    bundle: dict,
    *,
    source_language: str,
    target_language: str,
    context: dict,
    glossary: dict,
    require_reviewed=False,
    fallback=False,
    check=lambda: None,
) -> dict:
    report, replacements = quality(
        root,
        bundle,
        source_language=source_language,
        target_language=target_language,
        context=context,
        glossary=glossary,
        require_reviewed=require_reviewed,
        fallback=fallback,
    )

    def transform(staged):
        by_path = {}
        for item in replacements:
            by_path.setdefault(item["path"], []).append(item)
        for path, items in by_path.items():
            check()
            target = staged / path
            text = target.read_text()
            for item in sorted(items, key=lambda v: v["_start"], reverse=True):
                text = text[: item["_start"]] + item["value"] + text[item["_end"] :]
            target.write_text(text)
        # Language changes only the root attribute; identifiers and code stay intact.
        target = staged / "postgres.sgml"
        text = target.read_text()
        book = next(n for n in parse(text) if n.tag == "book")
        from .xml import ATTR

        opening = text[book.start : book.open_end]
        match = next(
            (m for m in ATTR.finditer(opening) if m[1].split("=")[0].strip() == "lang"), None
        )
        value = html.escape(target_language, quote=True)
        if match:
            opening = opening[: match.start(3)] + value + opening[match.end(3) :]
        else:
            opening = opening[:-1] + f' lang="{value}">'
        target.write_text(text[: book.start] + opening + text[book.open_end :])
        return {
            "api_version": "pg_maker/translation-result/v1",
            "quality": report,
            "validation": validate_book(staged),
        }

    return atomic_tree(
        root,
        destination,
        {
            "translator": "v1",
            "bundle": content_hash(bundle),
            "context": context,
            "fallback": fallback,
            "require_reviewed": require_reviewed,
        },
        transform,
        receipt_name=".pg-maker-translation.json",
        check=check,
    )
