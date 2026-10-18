"""Prepare an offline Sphinx Book Theme site from validated native DocBook HTML."""

import json
import posixpath
import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree, html

from pg_maker.errors import PreconditionError


def local_page(current, href, pages):
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    name = posixpath.normpath(posixpath.join(posixpath.dirname(current), unquote(parsed.path)))
    return name if name in pages else None


def hierarchy(pages):
    """Use native Up links and immediate TOC entries, in book rather than filename order."""
    parents, children = {}, {name: [] for name in pages}
    for name, document in pages.items():
        for href in document.xpath('//a[@accesskey="u"]/@href | //link[@rel="up"]/@href'):
            parent = local_page(name, href, pages)
            if parent and name != "index.html":
                parents[name] = parent
                break
        for href in document.xpath('//div[@class="toc"]/dl/dt//a/@href'):
            child = local_page(name, href, pages)
            if child and child != name and child not in children[name]:
                children[name].append(child)
    for name, items in children.items():
        for child in items:
            if child != "index.html":
                parents.setdefault(child, name)
    for name in pages:
        if name != "index.html":
            parents.setdefault(name, "index.html")
        seen, current = set(), name
        while current in parents:
            if current in seen:
                raise PreconditionError("cyclic DocBook navigation", details={"page": name})
            seen.add(current)
            current = parents[current]
    # The native next chain covers pages omitted from abbreviated chapter TOCs.
    order, current = [], "index.html"
    while current and current not in order:
        order.append(current)
        hrefs = pages[current].xpath('//link[@rel="next"]/@href | //a[@accesskey="n"]/@href')
        current = local_page(current, hrefs[0], pages) if hrefs else None
    order.extend(name for name in pages if name not in order)
    for name in children:
        children[name] = [child for child in children[name] if parents.get(child) == name]
    for name in order:
        if name in parents and name not in children[parents[name]]:
            children[parents[name]].append(name)
    return children


def prepare(
    source: Path,
    destination: Path,
    context: dict,
    language: str,
    *,
    branding: str = "vendor-metadata",
    links: dict | None = None,
):
    pages = {
        path.relative_to(source).as_posix(): html.document_fromstring(path.read_bytes())
        for path in sorted(source.rglob("*.html"))
    }
    if "index.html" not in pages:
        raise PreconditionError("Sphinx input has no DocBook entry point")
    children = hierarchy(pages)
    destination.mkdir(parents=True)
    theme = Path(__file__).parent / "theme"
    for directory in ("_static", "_templates"):
        shutil.copytree(theme / directory, destination / directory)
    shutil.copyfile(Path(__file__).with_name("sphinx_ext.py"), destination / "_pg_maker_docbook.py")
    assets = destination / "assets"
    assets.mkdir()
    for name, document in pages.items():
        body = document.find("body")
        if body is None:
            raise PreconditionError("DocBook HTML has no body")
        for navigation in body.xpath('.//div[@class="navheader" or @class="navfooter"]'):
            navigation.getparent().remove(navigation)
        target = destination / Path(name).with_suffix(".pgdoc")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "title": " ".join(document.xpath("//title/text()")) or name,
                    "html": etree.tostring(body, encoding="unicode", method="html"),
                    "children": [str(Path(child).with_suffix("")) for child in children[name]],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    for path in source.rglob("*"):
        if path.is_file() and path.suffix != ".html":
            target = assets / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    branded = branding != "none"
    title = " ".join(pages["index.html"].xpath("//title/text()"))
    metadata = {
        "product": context["vendor.product_name"] if branded else "PostgreSQL",
        "version": context["product.version"] if branded else context["postgresql.version"],
        "postgresql_version": context.get("postgresql.version", ""),
        "feature": context["feature.id"] if branded else "",
        "company": context.get("vendor.company", "") if branded else "",
        "language": language,
        "links": {},
    }
    if branded:
        for key, value in (links or {}).items():
            if value:
                parsed = urlsplit(value)
                if key not in {"homepage", "documentation_url"} or (
                    parsed.scheme not in {"http", "https"} or not parsed.netloc
                ):
                    raise PreconditionError("invalid documentation footer link")
                metadata["links"][key] = value
    config = {
        "project": title,
        "version": metadata["version"],
        "release": metadata["version"],
        "language": language,
        "html_theme": "sphinx_book_theme",
        "html_title": title,
        "html_static_path": ["_static"],
        "templates_path": ["_templates"],
        "html_extra_path": ["assets"],
        "html_css_files": ["pg-maker.css"],
        "html_js_files": ["pg-maker.js"],
        "exclude_patterns": ["assets"],
        "extensions": ["_pg_maker_docbook"],
        "source_suffix": {".pgdoc": "pg-maker-docbook"},
        "master_doc": "index",
        "html_show_sphinx": False,
        "html_show_copyright": False,
        "html_copy_source": False,
        "html_show_sourcelink": False,
        "html_context": {"maker": metadata},
        "html_theme_options": {
            "footer_start": ["footer_ru.html" if language == "ru" else "footer_en.html"],
            "footer_end": [],
            "use_download_button": False,
            "use_source_button": False,
            "show_navbar_depth": 1,
            "max_navbar_depth": 4,
            "show_toc_level": 2,
        },
    }
    (destination / "conf.py").write_text(
        "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
        + "\n".join(key + " = " + repr(value) for key, value in config.items())
        + "\n",
        encoding="utf-8",
    )
