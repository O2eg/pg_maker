import json
import runpy

import pytest
from lxml import html

from pg_maker.documentation.site import hierarchy, prepare
from pg_maker.errors import PreconditionError

CONTEXT = {
    "vendor.brand": "Example",
    "vendor.product_name": 'Example <DB> "Edition"',
    "vendor.company": "Example & Co",
    "feature.id": "analytics",
    "product.version": "19.0.2~beta4",
    "postgresql.version": "19beta4",
}


def page(title, body, *, up=None):
    return html.document_fromstring(
        f"<html><head><title>{title}</title></head><body>"
        + (f'<div class="navheader"><a accesskey="u" href="{up}">Up</a></div>' if up else "")
        + body
        + "</body></html>"
    )


def test_hierarchy_uses_book_order_and_nested_up_links():
    pages = {
        "index.html": page(
            "Book",
            '<div class="toc"><dl><dt><a href="z.html">First</a>'
            '</dt><dt><a href="a.html">Second</a></dt></dl></div>',
        ),
        "a.html": page("Second", "", up="index.html"),
        "z.html": page("First", "", up="index.html"),
        "sub/leaf.html": page("Leaf", "", up="../z.html"),
    }
    assert hierarchy(pages) == {
        "index.html": ["z.html", "a.html"],
        "a.html": [],
        "z.html": ["sub/leaf.html"],
        "sub/leaf.html": [],
    }
    pages["z.html"] = page("Cycle", "", up="sub/leaf.html")
    with pytest.raises(PreconditionError, match="cyclic"):
        hierarchy(pages)


@pytest.mark.parametrize("branding", ["vendor-metadata", "none"])
def test_site_default_theme_metadata_assets_and_literal_titles(tmp_path, branding):
    source = tmp_path / "html"
    source.mkdir()
    (source / "index.html").write_text(
        "<html><head><title>PostgreSQL *literal* Documentation</title></head>"
        '<body><div class="book" id="book"><h1>Book</h1><p>Body</p></div></body></html>'
    )
    (source / "image.svg").write_text("<svg/>")
    dest = tmp_path / "site"
    prepare(
        source,
        dest,
        CONTEXT,
        "en",
        branding=branding,
        links={"homepage": "https://example.org/?a=1&b=2", "documentation_url": None},
    )
    conf = runpy.run_path(str(dest / "conf.py"))
    assert conf["html_theme"] == "sphinx_book_theme"
    metadata = conf["html_context"]["maker"]
    assert metadata["product"] == (
        CONTEXT["vendor.product_name"] if branding != "none" else "PostgreSQL"
    )
    assert metadata["version"] == ("19.0.2~beta4" if branding != "none" else "19beta4")
    assert bool(metadata["links"]) == (branding != "none")
    assert metadata["feature"] == ("analytics" if branding != "none" else "")
    assert (dest / "assets/image.svg").is_file()
    assert (dest / "_templates/footer_en.html").is_file()
    assert (dest / "_static/pg-maker.js").is_file()
    data = json.loads((dest / "index.pgdoc").read_text())
    assert data["title"] == "PostgreSQL *literal* Documentation"
    assert 'id="book"' in data["html"]


def test_unsafe_footer_url_is_rejected(tmp_path):
    source = tmp_path / "html"
    source.mkdir()
    (source / "index.html").write_text(
        "<html><head><title>Book</title></head><body>Text</body></html>"
    )
    with pytest.raises(PreconditionError, match="footer link"):
        prepare(source, tmp_path / "site", CONTEXT, "en", links={"homepage": "javascript:alert(1)"})


def test_new_project_requests_theme_dependency(tmp_path):
    from pg_maker.assets import initialize_project
    from pg_maker.config import environment_requirements, load_config

    initialize_project(tmp_path / "project", with_doc=True)
    config = load_config(tmp_path / "project/build.yaml").data
    assert config["documentation"]["html_backend"] == "sphinx"
    build = environment_requirements(config)["environments"][0]
    assert "python3-sphinx-book-theme" in build["packages"]


@pytest.mark.parametrize("suffix", [".html", ".css", ".js", ".sgml"])
def test_theme_changes_invalidate_implementation_identity(tmp_path, monkeypatch, suffix):
    from pg_maker.planning import documents

    monkeypatch.setattr(documents, "resource_path", lambda: tmp_path)
    asset = tmp_path / ("theme" + suffix)
    asset.write_text("before")
    before = documents.implementation_identity()
    asset.write_text("after")
    assert documents.implementation_identity() != before
