"""Container-only Sphinx adapter: native HTML, semantic sections and searchable text.

Copied into the generated Sphinx source. It intentionally has no pg_maker imports.
"""

import json
import shutil
from pathlib import Path

from docutils import nodes
from lxml import etree, html
from sphinx import addnodes
from sphinx.parsers import Parser

SECTIONS = {
    "book",
    "part",
    "chapter",
    "appendix",
    "preface",
    "reference",
    "refentry",
    "bibliography",
    "index",
    "legalnotice",
    "section",
    "sect1",
    "sect2",
    "sect3",
    "sect4",
    "sect5",
    "refsect1",
    "refsect2",
    "refsect3",
    "refsection",
    "refsynopsisdiv",
    "simplesect",
}


class DocBookParser(Parser):
    supported = ("pg-maker-docbook",)

    def parse(self, inputstring, document):
        self.setup_parse(inputstring, document)
        data = json.loads(inputstring)
        body = html.document_fromstring(data["html"]).find("body")
        used = set(body.xpath(".//@id | .//a/@name"))
        counter = 0

        def raw(element):
            # Sphinx indexes text inside raw HTML and retains the original markup.
            return nodes.raw(
                "", etree.tostring(element, encoding="unicode", method="html"), format="html"
            )

        def section(element, title=None):
            nonlocal counter
            headings = element.xpath(
                './div[@class="titlepage"]//*[self::h1 or self::h2 or self::h3 '
                "or self::h4 or self::h5 or self::h6] | ./h1 | ./h2 | ./h3 | ./h4 | ./h5 "
                '| ./h6 | ./div[@class="refnamediv"]/h2'
            )
            heading = headings[0] if headings else None
            if title is None and heading is None:
                return raw(element)
            if heading is not None:
                for permalink in heading.xpath(".//a"):
                    if set(permalink.get("class", "").split()) & {
                        "idlink",
                        "id_link",
                        "headerlink",
                    }:
                        permalink.getparent().remove(permalink)
            title = title or " ".join(heading.itertext()).strip()
            ids = [element.get("id")] if element.get("id") else []
            if heading is not None:
                ids.extend(heading.xpath(".//@id | .//a/@name"))
                if heading.get("id"):
                    ids.append(heading.get("id"))
                heading.getparent().remove(heading)
            if not ids:
                counter += 1
                anchor = f"pg-maker-section-{counter}"
                while anchor in used:
                    counter += 1
                    anchor = f"pg-maker-section-{counter}"
                used.add(anchor)
                ids = [anchor]
            result = nodes.section(ids=list(dict.fromkeys(ids)), classes=["docbook-section"])
            document.note_explicit_target(result)
            result += nodes.title("", title)
            if element.text and element.text.strip():
                result += nodes.paragraph("", element.text)
            for child in element:
                if child.tag == "div" and set(child.get("class", "").split()) & SECTIONS:
                    result += section(child)
                else:
                    result += raw(child)
            return result

        roots = [e for e in body if set(e.get("class", "").split()) & SECTIONS]
        if len(roots) == 1:
            main = section(roots[0], data["title"])
            if body.get("id") and body.get("id") not in main["ids"]:
                main["ids"].append(body.get("id"))
            # Preserve any non-navigation content outside the DocBook root, too.
            for child in body:
                if child is not roots[0]:
                    main += raw(child)
        else:
            main = section(body, data["title"])
        if data["children"]:
            main += addnodes.toctree(
                entries=[(None, name) for name in data["children"]],
                includefiles=data["children"],
                maxdepth=-1,
                glob=False,
                hidden=True,
                includehidden=True,
                numbered=0,
                titlesonly=False,
                caption=None,
                parent=document.settings.env.docname,
            )
        document += main
        self.finish_parse()


def finalize_assets(app, exception):
    # Both themes copy internal Jinja macros into _static. They are build inputs,
    # not offline HTML pages, and retain unresolved template URLs if published.
    if exception is None and app.builder.format == "html":
        for name in ("webpack-macros.html", "sbt-webpack-macros.html"):
            (Path(app.outdir) / "_static" / name).unlink(missing_ok=True)
        # Debian shares Font Awesome assets through symlinks. The theme exposes
        # only some fonts, while its CSS also references the v4 compatibility font.
        # Copy the installed font set beside that CSS; pip layouts work unchanged.
        for theme in app.builder.theme.get_theme_dirs():
            css = Path(theme) / "static/vendor/fontawesome/css/all.min.css"
            if css.is_file():
                fonts = css.resolve().parent.parent / "webfonts"
                destination = Path(app.outdir) / "_static/vendor/fontawesome/webfonts"
                shutil.copytree(fonts, destination, dirs_exist_ok=True)


def setup(app):
    app.add_source_parser(DocBookParser)
    app.connect("build-finished", finalize_assets)
    return {"version": "1", "parallel_read_safe": True, "parallel_write_safe": True}
