"""Strict DocBook parsing with source spans; untouched XML is never reserialized."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from lxml import etree

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError, ValidationError

NAME = r"[A-Za-z_][\w.:-]*"
ENTITY = re.compile(r"&(" + NAME + r");")
ATTR = re.compile(r"(" + NAME + r"\s*=\s*)([\"'])(.*?)\2", re.S)
DTD_URL = "http://www.oasis-open.org/docbook/xml/4.5/docbookx.dtd"
DTD = Path("/usr/share/xml/docbook/schema/dtd/4.5/docbookx.dtd")


def path_in(root: Path, name: str) -> Path:
    if not relative_output(name):
        raise ValidationError("documentation path must be relative", details={"path": name})
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValidationError("documentation path escapes its tree", details={"path": name})
    return path


def markup(text: str):
    """Token boundaries only; the XML parser, not this scanner, validates structure."""
    pos = 0
    while (start := text.find("<", pos)) >= 0:
        ending = next(
            (
                end
                for prefix, end in (("<!--", "-->"), ("<![CDATA[", "]]>"), ("<?", "?>"))
                if text.startswith(prefix, start)
            ),
            None,
        )
        if ending:
            end = text.find(ending, start + 2)
            if end < 0:
                raise ValidationError("unterminated XML markup")
            pos = end + len(ending)
        else:
            quote, depth, pos = None, 0, start + 1
            while pos < len(text):
                c = text[pos]
                if quote:
                    if c == quote:
                        quote = None
                elif c in "\"'":
                    quote = c
                elif c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                elif c == ">" and depth == 0:
                    pos += 1
                    break
                pos += 1
            else:
                raise ValidationError("unterminated XML tag")
        yield start, pos, text[start:pos]


@dataclass
class Node:
    tag: str
    attrs: dict
    start: int
    open_end: int
    close_start: int
    end: int
    parent: Node | None = None
    children: list[Node] = field(default_factory=list)


def parse(text: str) -> list[Node]:
    tokens = list(markup(text))
    # Fragments may contain entities declared in another file. Keep entity nodes
    # opaque here; the whole book is subsequently checked against the local DTD.
    names = sorted(set(ENTITY.findall(text)) - {"amp", "lt", "gt", "quot", "apos"})
    clean = text
    for start, end, token in reversed(tokens):
        if token.startswith(("<!DOCTYPE", "<!ENTITY", "<?xml")):
            clean = clean[:start] + " " * (end - start) + clean[end:]
    declarations = "".join(f'<!ENTITY {name} "">' for name in names)
    wrapped = f"<!DOCTYPE maker [{declarations}]><maker>{clean}</maker>"
    try:
        parsed = etree.fromstring(
            wrapped.encode(),
            etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, recover=False),
        )
    except etree.XMLSyntaxError as exc:
        raise ValidationError("malformed documentation XML", details={"error": str(exc)}) from exc
    elements = iter(e for e in parsed.iterdescendants() if isinstance(e.tag, str))
    nodes, stack = [], []
    for start, end, token in tokens:
        if token.startswith(("<!", "<?")):
            continue
        if token.startswith("</"):
            node = stack.pop()
            node.close_start, node.end = start, end
        else:
            tag = re.match("<(" + NAME + ")", token)[1]
            attrs = dict(next(elements).attrib)
            node = Node(tag, attrs, start, end, end, end, stack[-1] if stack else None)
            if stack:
                stack[-1].children.append(node)
            nodes.append(node)
            if not token.endswith("/>"):
                stack.append(node)
    return nodes


def select(nodes: list[Node], selector: dict) -> list[Node]:
    if not selector or set(selector) - {"id", "tag", "parent"}:
        raise ValidationError("unsupported documentation selector")

    def match(node, value):
        return (
            ("id" not in value or node.attrs.get("id") == value["id"])
            and ("tag" not in value or node.tag == value["tag"])
            and (
                "parent" not in value
                or node.parent is not None
                and match(node.parent, value["parent"])
            )
        )

    return [node for node in nodes if match(node, selector)]


class LocalResolver(etree.Resolver):
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.catalog = DTD.parent.resolve()

    def resolve(self, url, public_id, context):
        if url == DTD_URL:
            path = DTD
        else:
            parsed = urlparse(url)
            if parsed.scheme not in {"", "file"} or parsed.netloc:
                raise ValidationError("network entity is forbidden", details={"url": url})
            path = Path(unquote(parsed.path))
            if not path.is_absolute():
                path = self.root / path
        resolved = path.resolve()
        # Debian's pinned DocBook package contains catalog-owned symlinks (notably
        # dbgenent.mod in /etc/sgml). Only normalized paths inside that catalog may
        # follow them; source-tree symlinks never gain this exception.
        catalog_path = Path(os.path.abspath(path)).is_relative_to(self.catalog)
        iso_entities = Path("/usr/share/xml/entities/xml-iso-entities-8879.1986").resolve()
        if not (
            resolved.is_relative_to(self.root)
            or catalog_path
            or resolved.is_relative_to(iso_entities)
        ):
            raise ValidationError(
                "entity escapes source and declared catalog", details={"url": url}
            )
        if not resolved.is_file():
            raise PreconditionError("missing documentation entity", details={"path": str(path)})
        return self.resolve_filename(str(resolved), context)


def validate_book(root: Path, *, entry="postgres.sgml") -> dict:
    path = path_in(root, entry)
    declaration = next(
        (value for _, _, value in markup(path.read_text()) if value.startswith("<!DOCTYPE")), ""
    )
    head = declaration.split("[", 1)[0]
    if DTD_URL not in head or "-//OASIS//DTD DocBook XML V4.5//EN" not in head:
        raise ValidationError("documentation requires the declared DocBook XML 4.5 adapter")
    parser = etree.XMLParser(
        load_dtd=True, dtd_validation=True, resolve_entities=True, no_network=True, recover=False
    )
    parser.resolvers.add(LocalResolver(root))
    try:
        book = etree.parse(str(path.resolve()), parser)
    except etree.XMLSyntaxError as exc:
        raise ValidationError("DocBook validation failed", details={"error": str(exc)}) from exc
    ids = [e.get("id") for e in book.iter() if isinstance(e.tag, str) and e.get("id")]
    if len(ids) != len(set(ids)):
        raise ValidationError("duplicate documentation ID")
    identifiers = set(ids)
    missing = sorted(
        {
            ref
            for e in book.iter()
            if isinstance(e.tag, str)
            for key in ("linkend", "endterm", "linkends")
            for ref in e.get(key, "").split()
            if ref not in identifiers
        }
    )
    if missing:
        raise ValidationError("dangling documentation references", details={"ids": missing[:30]})
    return {
        "passed": True,
        "ids": len(ids),
        "root": book.getroot().tag,
        "validator": "lxml-local-docbook-4.5",
    }
