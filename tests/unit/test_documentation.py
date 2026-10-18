import copy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_maker.documentation import translation, writer, xml
from pg_maker.documentation.install import collect_docs
from pg_maker.documentation.validation import html_tree
from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, file_hash, write_json

CORPUS = Path(__file__).parents[1] / "fixtures/stage7"
CONTEXT = {
    "vendor.brand": "Test",
    "vendor.product_name": "Test",
    "product.version": "18.1.0",
    "feature.id": "my-feature",
}


@pytest.fixture
def book(tmp_path, monkeypatch):
    # The unit validator uses a small local DTD. Docker tests use the real DocBook 4.5 catalog.
    source = tmp_path / "source"
    shutil.copytree(CORPUS / "18.1", source)
    tags = {n.tag for path in source.glob("*.sgml") for n in xml.parse(path.read_text())}
    dtd = tmp_path / "catalog/docbookx.dtd"
    dtd.parent.mkdir()
    dtd.write_text(
        "\n".join(
            f"<!ELEMENT {tag} ANY>\n<!ATTLIST {tag} id ID #IMPLIED lang CDATA #IMPLIED "
            "xreflabel CDATA #IMPLIED linkend IDREF #IMPLIED cols CDATA #IMPLIED>"
            for tag in tags | {"appendix"}
        )
    )
    monkeypatch.setattr(xml, "DTD", dtd)
    return source


def rules(*items):
    return [
        {
            "api_version": "pg_maker/doc-rules/v1",
            "kind": "DocumentationRules",
            "metadata": {"id": "fixture", "revision": 1},
            "spec": {"rules": list(items)},
        }
    ]


@pytest.mark.parametrize("version", ["17.5", "18.1"])
def test_writer_golden_preserves_entities_code_and_untouched_files(book, tmp_path, version):
    shutil.copytree(CORPUS / version, book, dirs_exist_ok=True)
    document = rules(
        {
            "id": "title",
            "action": "set-text",
            "file": "config.sgml",
            "selector": {"tag": "title", "parent": {"id": "settings"}},
            "expect_matches": 1,
            "text": "Настройки сервера ${vendor.brand}",
        }
    )
    result = writer.apply(book, tmp_path / "out", document, CONTEXT, assets={}, branding=False)
    assert (tmp_path / "out/config.sgml").read_bytes() == (
        CORPUS / (version + "-golden.sgml")
    ).read_bytes()
    for path in book.iterdir():
        if path.name != "config.sgml":
            assert path.read_bytes() == (tmp_path / "out" / path.name).read_bytes()
    assert result["rules"][0]["matches"] == 1
    assert '+<chapter id="settings"><title>Настройки сервера Test</title>' in result["diff"]
    assert (
        writer.apply(book, tmp_path / "out", document, CONTEXT, assets={}, branding=False) == result
    )
    with pytest.raises(PreconditionError, match="clean input"):
        writer.apply(
            tmp_path / "out", tmp_path / "twice", document, CONTEXT, assets={}, branding=False
        )


def test_writer_atomic_failure_and_dangling_reference(book, tmp_path):
    remove = {
        "id": "remove",
        "action": "remove",
        "file": "config.sgml",
        "selector": {"id": "guc-client-min-messages"},
        "expect_matches": 1,
    }
    with pytest.raises(ValidationError, match="validation"):
        writer.apply(book, tmp_path / "bad", rules(remove), CONTEXT, assets={}, branding=False)
    assert not (tmp_path / "bad").exists()
    remove["expect_matches"] = 2
    with pytest.raises(PreconditionError, match="cardinality"):
        writer.apply(book, tmp_path / "bad", rules(remove), CONTEXT, assets={}, branding=False)


def test_writer_adds_wired_page_and_escapes_variables(book, tmp_path):
    fragment = tmp_path / "page.sgml"
    fragment.write_text(
        '<appendix id="release-notes"><title>${vendor.brand} release notes</title>'
        "<para>New feature.</para></appendix>"
    )
    reference = {"input": "page", "sha256": file_hash(fragment)}
    rule = {
        "id": "notes",
        "action": "release-notes",
        "file": "notes.sgml",
        "selector": {"id": "postgres"},
        "expect_matches": 0,
        "fragment": reference,
        "entity": "notes",
        "include_file": "postgres.sgml",
    }
    writer.apply(
        book,
        tmp_path / "out",
        rules(rule),
        {**CONTEXT, "vendor.brand": 'A & B <"x">'},
        assets={"page": fragment},
        branding=False,
    )
    assert "A &amp; B &lt;&quot;x&quot;&gt;" in (tmp_path / "out/notes.sgml").read_text()
    assert "&notes;" in (tmp_path / "out/postgres.sgml").read_text()


@pytest.mark.parametrize(
    "target", ["file:///etc/passwd", "https://example.invalid/entity", "../outside.sgml"]
)
def test_entity_resolution_is_confined_to_tree_and_catalog(book, tmp_path, target):
    (tmp_path / "outside.sgml").write_text("secret")
    (book / "sections.sgml").write_text(
        f'<!ENTITY settings SYSTEM "{target}"><!ENTITY reference SYSTEM "reference.sgml">'
    )
    with pytest.raises((ValidationError, PreconditionError)):
        xml.validate_book(book)


def completed_bundle(book):
    bundle = translation.extract(book, source_language="en", target_language="ru", context=CONTEXT)
    for item in bundle["spec"]["segments"]:
        # Unit-only synthetic text; no semantic translation claim.
        item.update(
            translation="Перевод " + item["source"],
            state="translated",
            provenance={"author": "unit-fixture"},
        )
    return bundle


def test_translation_preserves_structure_code_entities_and_is_repeatable(book, tmp_path):
    bundle = completed_bundle(book)
    options = dict(
        source_language="en",
        target_language="ru",
        context=CONTEXT,
        glossary={"revision": "1", "terms": {}},
    )
    result = translation.apply(book, tmp_path / "translated", bundle, **options)
    assert result["quality"]["complete"]
    assert result["quality"]["semantic_accuracy"] == "not-proven"
    assert 'lang="ru"' in (tmp_path / "translated/postgres.sgml").read_text()
    original = (book / "config.sgml").read_text()
    translated = (tmp_path / "translated/config.sgml").read_text()
    assert (
        "<programlisting>SELECT 1; -- keep whitespace\n  SELECT 2;</programlisting>" in translated
    )
    assert "<literal>WARNING</literal>" in translated
    assert {n.attrs.get("id") for n in xml.parse(original)} == {
        n.attrs.get("id") for n in xml.parse(translated)
    }
    assert translation.apply(book, tmp_path / "translated", bundle, **options) == result


def test_translation_rejects_missing_stale_and_changed_placeholders(book):
    bundle = completed_bundle(book)
    options = dict(
        source_language="en",
        target_language="ru",
        context=CONTEXT,
        glossary={"revision": "1", "terms": {}},
    )
    missing = copy.deepcopy(bundle)
    missing["spec"]["segments"][0].update(state="missing", translation=None)
    with pytest.raises(PreconditionError, match="incomplete"):
        translation.quality(book, missing, **options)
    report, _ = translation.quality(book, missing, fallback=True, **options)
    assert report["fallback"] and not report["complete"]
    bad = copy.deepcopy(bundle)
    item = next(s for s in bad["spec"]["segments"] if s["placeholders"])
    item["translation"] = item["translation"].replace(next(iter(item["placeholders"])), "")
    with pytest.raises(ValidationError, match="placeholders"):
        translation.quality(book, bad, **options)
    bad = copy.deepcopy(bundle)
    bad["spec"]["segments"][0]["translation"] = bad["spec"]["segments"][0]["source"]
    with pytest.raises(ValidationError, match="source copy"):
        translation.quality(book, bad, **options)
    path = book / "config.sgml"
    path.write_text(path.read_text().replace("Server settings", "Changed settings"))
    with pytest.raises(PreconditionError, match="stale"):
        translation.quality(book, bundle, fallback=True, **options)


def test_html_validation_checks_local_assets_and_anchors(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html lang="ru"><body>Test 18.1.0 my-feature <a href="page.html#ok">link</a></body></html>'
    )
    page = tmp_path / "page.html"
    page.write_text('<html><body id="ok">Текст</body></html>')
    assert html_tree(tmp_path, language="ru", expected=["Test", "18.1.0", "my-feature"])["offline"]
    page.write_text('<html><body id="ok"><img src="https://example.invalid/a.png"></body></html>')
    with pytest.raises(PreconditionError, match="network"):
        html_tree(tmp_path, language="ru", expected=[])
    page.write_text('<html><body id="wrong">Текст</body></html>')
    with pytest.raises(PreconditionError, match="anchor"):
        html_tree(tmp_path, language="ru", expected=[])


def test_rules_reject_missing_action_fields_before_execution():
    document = rules({"id": "edit", "action": "set-text", "file": "x.sgml", "expect_matches": 1})
    with pytest.raises(ValidationError):
        validate_document(document[0], "doc-rules")


def test_css_import_requires_local_file(tmp_path):
    (tmp_path / "index.html").write_text(
        '<html lang="en"><head><link rel="stylesheet" href="style.css"></head></html>'
    )
    css = tmp_path / "style.css"
    css.write_text('@import "https://example.invalid/theme.css";')
    with pytest.raises(PreconditionError, match="network"):
        html_tree(tmp_path, language="en", expected=[])
    css.write_text('@import "missing.css";')
    with pytest.raises(PreconditionError, match="broken local"):
        html_tree(tmp_path, language="en", expected=[])


def test_manpage_ownership_distinguishes_server_apis_and_libpq(tmp_path):
    output, staging = tmp_path / "output", tmp_path / "staging"
    root = output / "docs/en/man/man3"
    root.mkdir(parents=True)
    for name in ("SPI_connect", "dblink", "PQconnectdb"):
        (root / (name + ".3")).write_text('.TH "' + name + '" "3"\n')
    commands = output / "docs/en/man/man1"
    commands.mkdir()
    for name in ("psql", "ecpg"):
        (commands / (name + ".1")).write_text('.TH "' + name + '" "1"\n')
    document = {
        "entries": inventory(output / "docs")["entries"],
        "context": CONTEXT,
        "profile": {"languages": ["en"], "formats": ["man"]},
    }
    document["manifest_hash"] = content_hash(document)
    write_json(output / "docs-manifest.json", document)
    branch = SimpleNamespace(
        output=output,
        prefix="/opt/test",
        variant={"identity": {"paths": {"documentation_directory": "/opt/test/share/doc"}}},
    )
    roles = {"opt/test/bin/psql": "client"}
    collect_docs(branch, staging, roles)
    assert roles["opt/test/share/man/man3/SPI_connect.3"] == "server"
    assert roles["opt/test/share/man/man3/dblink.3"] == "server"
    assert roles["opt/test/share/man/man3/PQconnectdb.3"] == "libpq"
    assert roles["opt/test/share/man/man1/psql.1"] == "client"
    assert not (staging / "opt/test/share/man/man1/ecpg.1").exists()


def test_pg19_doc_fix_prevents_html_case_collision_and_preserves_release_link(book, tmp_path):
    from pg_maker.postgresql import documentation_fixes, recipe

    (book / "ref").mkdir()
    (book / "ref/psql-ref.sgml").write_text(
        '<chapter id="psql"><title>psql</title>'
        '<para id="app-psql-prompting-S">%S</para>'
        '<para id="app-psql-prompting-s">%s</para>'
        '<para><link linkend="app-psql-prompting-S">Release note</link></para></chapter>'
    )
    # Reuse the local DTD fixture and wire the new chapter into the actual book.
    with (book / "sections.sgml").open("a") as stream:
        stream.write('\n<!ENTITY psql SYSTEM "ref/psql-ref.sgml">\n')
    path = book / "postgres.sgml"
    path.write_text(path.read_text().replace("</book>", "&psql;</book>"))
    selected = recipe("19beta4")
    writer.apply(
        book,
        tmp_path / "fixed",
        documentation_fixes("19beta4", selected),
        CONTEXT,
        assets={},
        branding=False,
    )
    raw = (tmp_path / "fixed/ref/psql-ref.sgml").read_text()
    ids = [n.attrs["id"].upper() for n in xml.parse(raw) if "id" in n.attrs]
    assert len(ids) == len(set(ids))
    assert 'linkend="app-psql-prompting-S"' in raw
    assert 'id="app-psql-prompting-s-lc"' in raw
