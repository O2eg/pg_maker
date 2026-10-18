"""Offline checks shared by render and installed-documentation acceptance."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import html

from pg_maker.errors import PreconditionError


def css_references(text):
    return [
        *re.findall(r"url\(([^)]+)\)", text),
        *re.findall(r"@import\s+[\"']([^\"']+)[\"']", text, flags=re.I),
    ]


def html_tree(root: Path, *, language: str, expected: list[str]) -> dict:
    root = root.resolve()
    if not (root / "index.html").is_file():
        raise PreconditionError("documentation has no HTML entry point")
    pages, identifiers, references = {}, {}, []
    for path in sorted(root.rglob("*.html")):
        document = html.document_fromstring(path.read_bytes())
        pages[path] = document
        ids = [
            v
            for e in document.iter()
            for v in (e.get("id"), e.get("name") if e.tag == "a" else None)
            if v
        ]
        identifiers[path] = set(ids)
        # XHTML legacy anchors may repeat id/name on the same element, so compare IDs separately.
        explicit = [e.get("id") for e in document.iter() if e.get("id")]
        if len(explicit) != len(set(explicit)):
            raise PreconditionError("duplicate HTML anchor", details={"path": path.name})
        for element in document.iter():
            for attr in ("href", "src", "data", "poster"):
                value = element.get(attr)
                if value:
                    asset = (
                        attr != "href"
                        or element.tag in {"image", "use"}
                        or (
                            element.tag == "link"
                            and bool(
                                set(element.get("rel", "").split())
                                & {"stylesheet", "icon", "preload"}
                            )
                        )
                    )
                    references.append((path, value, asset))
            if element.get("srcset"):
                references.extend(
                    (path, part.strip().split()[0], True)
                    for part in element.get("srcset").split(",")
                    if part.strip()
                )
            for css in (element.get("style", ""), element.text if element.tag == "style" else ""):
                references.extend(
                    (path, value.strip(" \"'"), True) for value in css_references(css or "")
                )
    for path in root.rglob("*.css"):
        references.extend(
            (path, value.strip(" \"'"), True) for value in css_references(path.read_text())
        )
    for source, value, asset in references:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            if asset and parsed.scheme != "data":
                raise PreconditionError(
                    "documentation asset requires network", details={"url": value}
                )
            if parsed.scheme not in {"http", "https", "mailto", "ftp", "news", "data"}:
                raise PreconditionError("unsafe documentation link scheme", details={"url": value})
            continue
        target = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
        if not target.is_relative_to(root) or not target.exists():
            raise PreconditionError(
                "broken local documentation link",
                details={"page": str(source.relative_to(root)), "link": value},
            )
        if (
            parsed.fragment
            and target in identifiers
            and unquote(parsed.fragment) not in identifiers[target]
        ):
            raise PreconditionError(
                "broken HTML anchor", details={"page": target.name, "anchor": parsed.fragment}
            )
    entry = pages[root / "index.html"]
    lang = (
        entry.get("lang")
        or entry.get("{http://www.w3.org/XML/1998/namespace}lang")
        or entry.get("xml:lang")
    )
    if not lang:
        declared = entry.xpath('//div[@class="book"]/@lang')
        lang = declared[0] if len(declared) == 1 else None
    if lang != language:
        raise PreconditionError(
            "HTML language differs from requested language",
            details={"actual": lang, "expected": language},
        )
    text = entry.text_content()
    if any(value not in text for value in expected):
        raise PreconditionError("HTML entry point lacks product/feature/version metadata")
    return {
        "passed": True,
        "pages": len(pages),
        "references": len(references),
        "language": language,
        "offline": True,
    }


def man_tree(root: Path) -> dict:
    files = sorted(p for p in root.glob("man*/*") if p.is_file())
    if not files:
        raise PreconditionError("documentation man output is empty")
    for path in files:
        text = path.read_text()
        if not text.strip() or not (".TH " in text or text.startswith(".so ")):
            raise PreconditionError("invalid man page", details={"path": path.name})
        if text.startswith(".so "):
            target = (root / text.splitlines()[0][4:].strip()).resolve()
            if not target.is_relative_to(root.resolve()) or not target.is_file():
                raise PreconditionError("broken man page alias")
    return {"passed": True, "pages": len(files)}
