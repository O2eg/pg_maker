"""Real offline DocBook/Sphinx/FOP and DEB installation, with no PostgreSQL DB tests."""

import os
import re
import signal
import time
from pathlib import Path

import pytest
import yaml
from lxml import html
from test_build_cancellation import prepared_build, resolve_fixture_lock
from test_distribution import CONFIGURE

from pg_maker.config import environment_requirements, load_config
from pg_maker.documentation.translation import PLACEHOLDER
from pg_maker.packaging.deb import inspect_deb
from pg_maker.planning.documents import seal
from pg_maker.storage import content_hash, file_hash, read_json, write_json

pytestmark = pytest.mark.docker
CORPUS = Path(__file__).parents[1] / "fixtures/stage7"
DOC_MAKE = r"""srcdir ?= $(dir $(abspath $(firstword $(MAKEFILE_LIST))))
STYLES = /usr/share/xml/docbook/stylesheet/docbook-xsl
GENERATED_SGML = version.sgml
version.sgml:
	cp $(srcdir)/version.sgml $@
check:
	xmllint --nonet --valid --noout $(srcdir)/postgres.sgml
postgres-full.xml:
	xmllint --nonet --valid --noent --output $@ $(srcdir)/postgres.sgml
html: postgres-full.xml
	mkdir -p html
	cd html && xsltproc --nonet $(XSLTPROCFLAGS) $(STYLES)/html/chunk.xsl ../postgres-full.xml
man: postgres-full.xml
	xsltproc --nonet $(XSLTPROCFLAGS) --param man.output.in.separate.dir 1 \
	--stringparam man.output.base.dir "" $(STYLES)/manpages/docbook.xsl postgres-full.xml
postgres-A4.pdf: postgres-full.xml
	xsltproc --nonet $(XSLTPROCFLAGS) $(STYLES)/fo/docbook.xsl postgres-full.xml > postgres.fo
	$(FOP) -fo postgres.fo -pdf $@
"""


def resolve(service, root, environment):
    config = load_config(root / "build.yaml")
    images = read_json(environment)["spec"]["images"]
    write_json(
        root / "env.json",
        seal(
            "environment",
            {
                "requirements_hash": content_hash(environment_requirements(config.data)),
                "images": images,
            },
        ),
    )
    resolve_fixture_lock(service, root)


def project(
    tmp_path,
    monkeypatch,
    *,
    languages=None,
    backend="docbook",
    formats=None,
    extra_feature=False,
    version="18.1",
):
    environment = os.environ.get("PG_MAKER_DOC_ENVIRONMENTS")
    if not environment:
        pytest.skip("set PG_MAKER_DOC_ENVIRONMENTS to prepared documentation images")
    environment = Path(environment)
    image = next(
        i["image_id"] for i in read_json(environment)["spec"]["images"] if i["role"] == "build"
    )
    monkeypatch.setenv("PG_MAKER_BUILD_IMAGE", image)
    source_files = {"COPYRIGHT": "Fixture license\n", "doc/src/sgml/Makefile": DOC_MAKE}
    source_files.update(
        {
            "doc/src/sgml/" + path.name: path.read_text()
            for path in (CORPUS / version).glob("*.sgml")
        }
    )
    configure = CONFIGURE.replace("mkdir -p src", "mkdir -p src doc/src/sgml")
    service = prepared_build(
        tmp_path, configure, source_files=source_files, extra_feature=extra_feature
    )
    profile = yaml.safe_load((tmp_path / "config/documentation.yaml").read_text())
    profile["spec"].update(
        enabled=True,
        languages=languages or ["en"],
        formats=formats or ["html", "man"],
        html_backend=backend,
    )
    (tmp_path / "config/documentation.yaml").write_text(yaml.safe_dump(profile, sort_keys=False))
    resolve(service, tmp_path, environment)
    return service, environment


def wait(service, started, *, status="succeeded"):
    directory = service.directory / started["operation_id"]
    state = service.wait(directory, timeout_seconds=240)
    assert state["status"] == status, state
    assert not state["resources"]
    return directory, state


@pytest.mark.parametrize("version", ["17.5", "18.1"])
def test_docbook_html_man_pdf_offline(tmp_path, monkeypatch, version):
    service, _ = project(tmp_path, monkeypatch, version=version, formats=["html", "man", "pdf"])
    _, state = wait(service, service.build(tmp_path / "lock.json", goal="docs"))
    output = Path(state["result"]["reports"][0]["path"]).parent
    manifest = read_json(output / "docs-manifest.json")
    assert manifest["languages"]["en"]["quality"]["translation"] == "not-required"
    assert set(manifest["languages"]["en"]["validation"]) == {"html", "man", "pdf"}
    assert len(list(output.glob("*.tar.xz"))) == 3
    assert state["result"]["db_tests"] == "skipped"
    # Earlier resolutions must remain exportable after the documentation profile changes.
    for request in service.directory.glob("*/input.json"):
        if read_json(request)["operation_kind"] != "resolve_inputs":
            continue
        destination = tmp_path / "resolve-exports" / request.parent.name
        service.export(request.parent, destination)
        expected = service.status(request.parent)["result"]["sha256"]
        assert file_hash(destination / "prepared-document.json") == expected


def test_docs_package_has_weak_dependency_and_installed_offline_files(tmp_path, monkeypatch):
    service, _ = project(tmp_path, monkeypatch, formats=["html", "man", "pdf"])
    _, state = wait(service, service.build(tmp_path / "lock.json", goal="package"))
    output = Path(state["result"]["reports"][0]["path"]).parent
    packages = {p["role"]: p for p in read_json(output / "packages.json")}
    assert set(packages) == {"server", "client", "libpq", "docs"}
    server = inspect_deb(output / packages["server"]["path"])["control"]
    assert packages["docs"]["name"] in server["Suggests"]
    assert packages["docs"]["name"] not in server["Depends"]
    docs = inspect_deb(output / packages["docs"]["path"])
    assert "Depends" not in docs["control"]
    assert not {"postinst", "prerm", "postrm"} & docs["scripts"].keys()
    assert any(e["path"].endswith("/en/html/index.html") for e in docs["entries"])
    assert any(e["path"].endswith("/en/pdf/manual.pdf") for e in docs["entries"])
    client = inspect_deb(output / packages["client"]["path"])
    assert any(e["path"].endswith("/share/man/man1/psql.1") for e in client["entries"])
    results = read_json(output / "package-tests.json")
    assert not results["run_database"]
    assert {r["set"] for r in results["sets"]} == {"server", "client", "libpq", "docs"}
    assert all("installed-documentation" in r["checks"] for r in results["sets"])
    assert not read_json(output / "artifact-manifest.json")["publication_eligible"]


def test_missing_translation_exports_segments_and_fails_without_english_fallback(
    tmp_path, monkeypatch
):
    service, _ = project(tmp_path, monkeypatch, languages=["ru"])
    _, state = wait(service, service.build(tmp_path / "lock.json", goal="docs"), status="failed")
    output = Path(state["result"]["reports"][0]["path"]).parent
    assert read_json(output / "translations/ru.json")["spec"]["segments"]
    assert not (output / "artifact-manifest.json").exists()
    assert not (output / "docs").exists()


RUSSIAN = {
    "Documentation": "Документация",
    "Server settings": "Настройки сервера",
    "configuration parameter": "параметр конфигурации",
    "Controls which": "Определяет, какие",
    "message levels": "уровни сообщений",
    "are written to the server log. Valid values are": (
        "записываются в журнал сервера. Допустимые значения:"
    ),
    ", and": "и",
    (
        ". Each level includes all the levels that follow it. The later the level, "
        "the fewer messages are sent to the log. The default is"
    ): (
        ". Каждый уровень включает все следующие за ним уровни. Чем дальше уровень в списке, "
        "тем меньше сообщений попадает в журнал. Значение по умолчанию:"
    ),
    ". Note that": ". Обратите внимание:",
    "has a different rank here than in": "здесь имеет другой приоритет, чем в",
    ". Only superusers and users with the appropriate": (
        ". Изменять эту настройку могут только суперпользователи и пользователи "
        "с соответствующей привилегией"
    ),
    "privilege can change this setting.": ".",
    "Message levels.": "Уровни сообщений.",
    "Client message settings.": "Настройки сообщений клиента.",
    "Table text": "Текст таблицы",
    "Commands": "Команды",
    "interactive terminal": "интерактивный терминал",
    "Description": "Описание",
    "Run SQL commands.": "Выполняйте команды SQL.",
}


def import_fixture_bundle(path):
    bundle = read_json(path)
    for item in bundle["spec"]["segments"]:
        chunks = re.split(r"(\{\{p[0-9]{4,}\}\})", item["source"])
        output = []
        for chunk in chunks:
            if PLACEHOLDER.fullmatch(chunk) or not any(char.isalpha() for char in chunk):
                output.append(chunk)
            else:
                value = re.sub(r"\s+", " ", chunk).strip()
                output.append(
                    (" " if chunk[0].isspace() else "")
                    + RUSSIAN[value]
                    + (" " if chunk[-1].isspace() else "")
                )
        item.update(
            translation="".join(output),
            state="reviewed",
            provenance={"author": "pg-maker-fixture", "revision": "1"},
        )
    return bundle


def test_sphinx_ru_en_two_features_and_translation_bundles(tmp_path, monkeypatch):
    service, environment = project(
        tmp_path,
        monkeypatch,
        languages=["en", "ru"],
        backend="sphinx",
        formats=["html", "man", "pdf"],
        extra_feature=True,
    )
    chapter = tmp_path / "source/doc/src/sgml/config.sgml"
    chapter.write_text(
        chapter.read_text().replace(
            "</chapter>",
            '<sect1 id="settings-detail"><title>Message levels.</title>'
            "<para>Table text</para></sect1></chapter>",
        )
    )
    resolve(service, tmp_path, environment)
    _, extracted = wait(service, service.build(tmp_path / "lock.json", goal="translations"))
    references = []
    for report in extracted["result"]["reports"]:
        output = Path(report["path"]).parent
        bundle = import_fixture_bundle(output / "translations/ru.json")
        name = "translations-" + bundle["spec"]["context"]["feature.id"] + ".json"
        write_json(tmp_path / "config" / name, bundle)
        references.append(name)
    profile = yaml.safe_load((tmp_path / "config/documentation.yaml").read_text())
    profile["spec"]["translations"] = references
    (tmp_path / "config/documentation.yaml").write_text(yaml.safe_dump(profile, sort_keys=False))
    resolve(service, tmp_path, environment)
    _, state = wait(service, service.build(tmp_path / "lock.json", goal="docs"))
    assert len(state["result"]["reports"]) == 2
    for report in state["result"]["reports"]:
        output = Path(report["path"]).parent
        manifest = read_json(output / "docs-manifest.json")
        assert manifest["languages"]["ru"]["quality"]["complete"]
        assert manifest["languages"]["ru"]["quality"]["reviewed"] > 0
        assert manifest["profile"]["html_backend"] == "sphinx"
        assert "Документация" in (output / "docs/ru/html/index.html").read_text()
        assert "Documentation" in (output / "docs/en/html/index.html").read_text()
        assert 'lang="ru"' in (output / "docs/ru/html/index.html").read_text()
        for language in ("en", "ru"):
            root = output / "docs" / language / "html"
            page = html.parse(str(root / "index.html"))
            assert page.xpath('//*[@id="pst-primary-sidebar"]//a[@href="ch01.html"]')
            footer = page.xpath('//div[@class="pg-maker-footer"]')[0]
            assert manifest["context"]["product.version"] in footer.text_content()
            assert manifest["context"]["feature.id"] in footer.text_content()
            assert language.upper() in footer.text_content()
            assert not footer.xpath(".//a")  # Default vendor has no website links.
            assert len(page.xpath("//article//h1")) == 1
            assert " ".join(footer.xpath("./strong")[0].text_content().split()) == (
                manifest["context"]["vendor.product_name"]
                + " "
                + manifest["context"]["product.version"]
            )
            chapter_page = html.parse(str(root / "ch01.html"))
            assert len(chapter_page.xpath("//article//h1")) == 1
            assert chapter_page.xpath(
                '//*[@id="pst-secondary-sidebar"]//a[@href="#settings-detail"]'
            )
            # Body words, not only page titles, must reach Sphinx's search index.
            if language == "en":
                assert "whitespac" in (root / "searchindex.js").read_text()


def test_docs_resume_discards_partial_render_and_parallel_runs_are_isolated(tmp_path, monkeypatch):
    service, environment = project(tmp_path, monkeypatch, formats=["html"])
    make = tmp_path / "source/doc/src/sgml/Makefile"
    make.write_text(
        make.read_text().replace(
            "html: postgres-full.xml\n", "html: postgres-full.xml\n\ttouch rendering\n\tsleep 5\n"
        )
    )
    resolve(service, tmp_path, environment)
    first = service.build(tmp_path / "lock.json", goal="docs")
    second = service.build(tmp_path / "lock.json", goal="docs")
    directory = service.directory / first["operation_id"]
    try:
        deadline = time.monotonic() + 30
        marker = (
            directory
            / "workspace/build/my-feature.debian13-x64.candidate/targets/debian13-x64"
            / "docs/my-feature/en/obj/rendering"
        )
        while not marker.exists():
            assert time.monotonic() < deadline
            assert service.status(directory)["status"] in {"running", "planned"}
            time.sleep(0.05)
        os.kill(first["owner"]["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while service.status(directory)["status"] != "interrupted":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        plan_hash = read_json(directory / "input.json")["plan"]["plan_hash"]
        _, resumed = wait(service, service.resume(directory, plan_hash=plan_hash))
        _, parallel = wait(service, second)
        assert resumed["attempt"] == 2
        assert parallel["attempt"] == 1
        assert '"type":"checkpoint_restored"' in (directory / "events.jsonl").read_text()
        assert list((directory / "attempts/2/previous").rglob("rendering"))
        for state in (resumed, parallel):
            output = Path(state["result"]["reports"][0]["path"]).parent
            assert read_json(output / "docs-manifest.json")["languages"]["en"]["validation"][
                "html"
            ]["passed"]
    finally:
        for started in (first, second):
            service.cancel(service.directory / started["operation_id"])
            service.wait(service.directory / started["operation_id"], timeout_seconds=30)


def test_pdf_svg_text_uses_java_font_manager(tmp_path):
    """Full upstream manuals contain SVG text, unlike the small DocBook corpus."""
    import subprocess

    environment = os.environ.get("PG_MAKER_DOC_ENVIRONMENTS")
    if not environment:
        pytest.skip("set PG_MAKER_DOC_ENVIRONMENTS to prepared documentation images")
    image = next(
        i["image_id"]
        for i in read_json(Path(environment))["spec"]["images"]
        if i["role"] == "build"
    )
    from pg_maker.documentation.builder import fop_config

    (tmp_path / "fop.xml").write_text(fop_config())
    (tmp_path / "svg.fo").write_text("""<fo:root xmlns:fo="http://www.w3.org/1999/XSL/Format">
<fo:layout-master-set><fo:simple-page-master master-name="page">
<fo:region-body/></fo:simple-page-master></fo:layout-master-set>
<fo:page-sequence master-reference="page"><fo:flow flow-name="xsl-region-body"><fo:block>
<fo:instream-foreign-object><svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">
<text x="10" y="25" font-family="Times,serif">PG maker SVG</text>
<text x="10" y="55" font-family="Courier">Courier SVG</text>
</svg></fo:instream-foreign-object></fo:block>
<fo:block font-family="Symbol">∑</fo:block>
</fo:flow></fo:page-sequence></fo:root>""")
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "sh",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--label",
            "io.pg-maker.test=pdf-svg",
            "-v",
            f"{tmp_path}:/probe",
            "-w",
            "/probe",
            image,
            "-ec",
            "fop -c fop.xml -fo svg.fo -pdf svg.pdf; "
            "pdffonts svg.pdf > fonts.txt; pdftotext svg.pdf -",
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "PG maker SVG" in result.stdout
    fonts = (tmp_path / "fonts.txt").read_text().splitlines()[2:]
    assert fonts and all(line.split()[-5] == "yes" for line in fonts), fonts


def test_sphinx_site_keeps_anchors_without_docbook_permalink_text(tmp_path):
    import subprocess

    from pg_maker.documentation.site import prepare
    from pg_maker.documentation.validation import html_tree

    environment = os.environ.get("PG_MAKER_DOC_ENVIRONMENTS")
    if not environment:
        pytest.skip("set PG_MAKER_DOC_ENVIRONMENTS to prepared documentation images")
    image = next(
        item["image_id"]
        for item in read_json(Path(environment))["spec"]["images"]
        if item["role"] == "build"
    )
    source = tmp_path / "raw"
    source.mkdir()
    (source / "index.html").write_text(
        '<html><head><title>Book</title></head><body><div class="book" id="BOOK">'
        '<div class="titlepage"><h1>Book</h1></div>'
        '<div class="sect1" id="DETAILS"><div class="titlepage"><h2>Details '
        '<a href="#DETAILS" class="id_link">#</a></h2></div>'
        '<p>Searchablequasar body text.</p><a href="#DETAILS">Details</a>'
        "</div></div></body></html>"
    )
    prepare(
        source,
        tmp_path / "site",
        {
            "vendor.product_name": "Example <DB>",
            "vendor.company": "Co & Co",
            "product.version": "19.0.2~beta4",
            "postgresql.version": "19beta4",
            "feature.id": "analytics",
        },
        "en",
        links={"homepage": "https://example.org/?a=1&b=2"},
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--entrypoint",
            "sphinx-build",
            "-v",
            f"{tmp_path}:/work",
            image,
            "-q",
            "-b",
            "html",
            "-W",
            "/work/site",
            "/work/rendered",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = tmp_path / "rendered"
    assert html_tree(output, language="en", expected=[])["passed"]
    document = html.parse(str(output / "index.html"))
    heading = document.xpath('//section[@id="DETAILS"]/h2')[0]
    assert heading.text == "Details"
    assert len(document.xpath("//article//h1")) == 1
    toc = document.xpath('//*[@id="pst-secondary-sidebar"]//a[@href="#DETAILS"]')[0]
    assert toc.text_content() == "Details"
    footer = document.xpath('//div[@class="pg-maker-footer"]')[0]
    assert "Example <DB> 19.0.2~beta4" in footer.text_content()
    assert "Co & Co" in footer.text_content()
    assert not footer.xpath(".//db")
    assert footer.xpath(".//a/@href") == ["https://example.org/?a=1&b=2"]
    assert "searchablequasar" in (output / "searchindex.js").read_text()
