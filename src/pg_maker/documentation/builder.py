"""DocBook/Sphinx steps within BuildBranch; no scheduler, credentials or network."""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from pg_maker.config import resource_path
from pg_maker.errors import CommandError, PreconditionError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, file_hash, read_json, write_json


def context(branch):
    value = {
        "vendor." + key: branch.config["vendor"][key]
        for key in ("brand", "product_name", "company", "slug")
    }
    value.update(
        {
            "product.version": branch.variant["identity"]["product_version"],
            "postgresql.version": branch.config["postgresql"]["version"],
            "feature.id": branch.variant["feature"],
            "feature.revision": str(branch.feature["metadata"]["revision"]),
            "target.id": branch.variant["target"],
            "components": content_hash(branch.feature["spec"]["components"]),
        }
    )
    value.update({"paths." + key: val for key, val in branch.variant["identity"]["paths"].items()})
    return value


def container_path(branch, path):
    return "/work/" + path.relative_to(branch.context.directory / "workspace").as_posix()


def worker(branch, action, result, **arguments):
    branch.prepare()
    tools = branch.helper / "doc-tools/pg_maker"
    if not tools.exists():
        tools.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            resource_path(), tools, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    request = branch.root / "docs-requests" / (str(branch.counter + 1) + ".json")
    write_json(request, {"action": action, "result": container_path(branch, result), **arguments})
    try:
        branch.command(
            ["python3", "-m", "pg_maker.documentation.worker", container_path(branch, request)],
            env={"PYTHONPATH": "/work/source/doc-tools"},
        )
    except CommandError:
        error_path = result.with_suffix(".error.json")
        if error_path.is_file():
            error = read_json(error_path)
            raise PreconditionError(error["message"], details=error.get("details", {})) from None
        raise
    return read_json(result)


def paths(branch):
    return branch.root / "targets" / branch.variant["target"] / "docs" / branch.variant["feature"]


def prepare(branch):
    branch.prepare()
    root = paths(branch)
    root.mkdir(parents=True, exist_ok=True)
    helper = branch.helper / "docs-prerequisites.mk"
    adapter = branch.config["postgresql_recipe"]["documentation"]
    directory = adapter["source_directory"]
    helper.write_text(
        ".PHONY: maker-doc-prerequisites\nmaker-doc-prerequisites: $("
        + adapter["generated_variable"]
        + ")\n\t@true\n"
    )
    branch.command(
        [
            "make",
            "-f",
            branch.container_root + "/source/" + directory + "/Makefile",
            "-f",
            "/work/source/docs-prerequisites.mk",
            "maker-doc-prerequisites",
        ],
        cwd=branch.container_root + "/obj/" + directory,
    )
    raw = root / "raw"
    shutil.copytree(branch.source / directory, raw, symlinks=True)
    # Generated tables/version are part of the fixed writer input, not stale output
    # borrowed from a different language or feature.
    for path in sorted((branch.obj / directory).glob("*.sgml")):
        shutil.copyfile(path, raw / path.name)
    profile = branch.config["documentation"]
    documents = [*profile["rules"], *branch.feature["spec"]["documentation"]["rules"]]
    for component in branch.feature["spec"]["components"]:
        documents.extend(component["spec"].get("documentation", {}).get("rules", []))
    assets = {}
    for document in documents:
        for rule in document["spec"]["rules"]:
            for key in ("fragment", "source"):
                if key in rule:
                    ref = rule[key]
                    destination = branch.helper / "doc-assets" / ref["sha256"].split(":")[1]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(branch.inputs.blob(ref["sha256"]), destination)
                    assets[ref["input"]] = container_path(branch, destination)
    result = worker(
        branch,
        "writer",
        branch.output / "documentation/writer.json",
        source=container_path(branch, raw),
        destination=container_path(branch, root / "written"),
        documents=documents,
        context=context(branch),
        assets=assets,
        branding=profile["branding"] == "vendor-metadata",
        source_language=profile["source_language"],
    )
    branch.report["documentation"] = {
        "source_hash": result["input_hash"],
        "transformed_hash": result["output_hash"],
        "context": context(branch),
        "languages": {},
        "writer": "v1",
        "translator": "v1",
    }


def translate(branch, language, extract_only):
    profile, root = branch.config["documentation"], paths(branch)
    arguments = {
        "root": container_path(branch, root / "written"),
        "source_language": profile["source_language"],
        "target_language": language,
        "context": context(branch),
        "glossary": profile.get("glossary", {"revision": "1", "terms": {}}),
    }
    destination = root / language / "source"
    if extract_only:
        bundle = worker(
            branch, "extract", branch.output / ("translations/" + language + ".json"), **arguments
        )
        branch.report["documentation"]["languages"][language] = {
            "status": "extracted",
            "segments": len(bundle["spec"]["segments"]),
        }
        return
    if language == profile["source_language"]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root / "written", destination, symlinks=True)
        quality = {"complete": True, "translation": "not-required"}
    else:
        ref = next(
            (
                r
                for r in profile["translations"]
                if r["language"] == language
                and r["feature"] == branch.variant["feature"]
                and r.get("target", branch.variant["target"]) == branch.variant["target"]
            ),
            None,
        )
        if ref is None:
            bundle = worker(
                branch,
                "extract",
                branch.output / ("translations/" + language + ".json"),
                **arguments,
            )
            raise PreconditionError(
                "missing translation bundle; run pg-maker docs prepare-translations LOCK "
                "and import the generated bundle",
                details={
                    "language": language,
                    "segments": len(bundle["spec"]["segments"]),
                    "bundle": "translations/" + language + ".json",
                },
            )
        bundle_path = branch.helper / "doc-assets" / ref["sha256"].split(":")[1]
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(branch.inputs.blob(ref["sha256"]), bundle_path)
        result = worker(
            branch,
            "translate",
            branch.output / ("documentation/translation-" + language + ".json"),
            destination=container_path(branch, destination),
            bundle=container_path(branch, bundle_path),
            require_reviewed=profile.get("require_reviewed", False),
            fallback=profile["missing_translation"] == "fallback",
            **arguments,
        )
        quality = result["quality"]
    branch.report["documentation"]["languages"][language] = {
        "status": "applied",
        "quality": quality,
    }


def fop_config():
    # Upstream FO and SVG figures name Base-14 fonts explicitly, bypassing
    # generic serif/sans/mono. Map those names too so the PDF is self-contained.
    families = {
        "DejaVuSerif": ["serif", "Times", "Times-Roman", "Times New Roman"],
        "DejaVuSans": ["sans-serif", "Helvetica", "Arial", "Symbol", "ZapfDingbats"],
        "DejaVuSansMono": ["monospace", "Courier", "Courier New"],
    }
    fonts = []
    for name, aliases in families.items():
        for bold in (False, True):
            for italic in (False, True):
                suffix = ("Bold" if bold else "") + (
                    ("Italic" if name == "DejaVuSerif" else "Oblique") if italic else ""
                )
                filename = name + ("-" + suffix if suffix else "")
                style = "italic" if italic else "normal"
                fonts.append(
                    f'<font embed-url="/usr/share/fonts/truetype/dejavu/{filename}.ttf">'
                    + "".join(
                        f'<font-triplet name="{alias}" style="{style}" '
                        f'weight="{"bold" if bold else "normal"}"/>'
                        for alias in aliases
                    )
                    + "</font>"
                )
    return (
        '<fop version="1.0"><strict-validation>true</strict-validation>'
        '<renderers><renderer mime="application/pdf"><fonts>'
        + "".join(fonts)
        + "</fonts></renderer></renderers></fop>"
    )


def build(branch, language):
    branch.prepare()
    profile, root = branch.config["documentation"], paths(branch)
    adapter = branch.config["postgresql_recipe"]["documentation"]
    source, obj = root / language / "source", root / language / "obj"
    obj.mkdir(parents=True)
    # The configure/generated prerequisites belong to this branch. Each renderer
    # gets a private obj and translated source; no shared mutable en/ru directory.
    make = [
        "make",
        "--output-sync=target",
        "-j",
        str(branch.variant["build"]["jobs"]),
        "-f",
        container_path(branch, source / "Makefile"),
        "top_builddir=" + branch.container_root + "/obj",
        "top_srcdir=" + branch.container_root + "/source",
        "srcdir=" + container_path(branch, source),
        "VPATH=" + container_path(branch, source),
        "XSLTPROCFLAGS=--stringparam l10n.gentext.language "
        + language
        + " --stringparam chunker.output.encoding UTF-8 --stringparam html.output.encoding UTF-8"
        # PostgreSQL's stylesheet emits a bare email as link/@href. Omit this
        # optional upstream contact metadata in the distribution's offline manual.
        + " --stringparam link.mailto.url ''",
    ]
    cwd = container_path(branch, obj)
    branch.command([*make, adapter["check_target"]], cwd=cwd)
    rendered = branch.output / "docs" / language
    rendered.mkdir(parents=True)
    validations = {}
    expected = (
        [context(branch)[key] for key in ("vendor.brand", "product.version", "feature.id")]
        if profile["branding"] != "none"
        else []
    )
    for fmt in profile["formats"]:
        branch.context.check()
        if fmt == "html":
            branch.command([*make, adapter["html_target"]], cwd=cwd)
            worker(
                branch,
                "html",
                branch.output / f"documentation/{language}-raw-html.json",
                root=container_path(branch, obj / "html"),
                language=language,
                expected=expected,
            )
            html_output = obj / "html"
            if profile["html_backend"] == "sphinx":
                site = root / language / "site"
                worker(
                    branch,
                    "site",
                    branch.output / f"documentation/{language}-site.json",
                    source=container_path(branch, html_output),
                    destination=container_path(branch, site),
                    context=context(branch),
                    language=language,
                    branding=profile["branding"],
                    links={
                        key: branch.config["vendor"].get(key)
                        for key in ("homepage", "documentation_url")
                    },
                )
                html_output = obj / "site-html"
                branch.command(
                    [
                        "sphinx-build",
                        "-j",
                        str(branch.variant["build"]["jobs"]),
                        "-b",
                        "html",
                        "-W",
                        "--keep-going",
                        "-d",
                        container_path(branch, root / language / "doctrees"),
                        container_path(branch, site),
                        container_path(branch, html_output),
                    ],
                    cwd=cwd,
                )
            shutil.copytree(html_output, rendered / "html", symlinks=True)
            validations[fmt] = worker(
                branch,
                "html",
                branch.output / f"documentation/{language}-html.json",
                root=container_path(branch, rendered / "html"),
                language=language,
                expected=expected,
            )
        elif fmt == "man":
            branch.command([*make, adapter["man_target"]], cwd=cwd)
            man = rendered / "man"
            man.mkdir()
            for directory in sorted(obj.glob("man[0-9]")):
                shutil.copytree(directory, man / directory.name, symlinks=True)
            validations[fmt] = worker(
                branch,
                "man",
                branch.output / f"documentation/{language}-man.json",
                root=container_path(branch, man),
            )
        elif fmt == "pdf":
            fop = branch.helper / "fop.xml"
            fop.write_text(fop_config())
            branch.command(
                [*make, "FOP=fop -c /work/source/fop.xml", adapter["pdf_target"]],
                cwd=cwd,
                env={"JAVA_ARGS": "-Xmx1500m"},
            )
            pdf = rendered / "pdf"
            pdf.mkdir()
            shutil.copyfile(obj / adapter["pdf_target"], pdf / "manual.pdf")
            info = branch.command(["pdfinfo", container_path(branch, pdf / "manual.pdf")])
            fonts = branch.command(["pdffonts", container_path(branch, pdf / "manual.pdf")])
            branch.command(
                [
                    "pdftotext",
                    container_path(branch, pdf / "manual.pdf"),
                    container_path(branch, obj / "manual.txt"),
                ]
            )
            text = (obj / "manual.txt").read_text()
            font_lines = Path(fonts.stdout_log).read_text().splitlines()[2:]
            if (
                any(value not in text for value in expected)
                or not font_lines
                or any(len(line.split()) < 6 or line.split()[-5] != "yes" for line in font_lines)
            ):
                raise PreconditionError("PDF lacks expected product text or embedded fonts")
            validations[fmt] = {
                "passed": True,
                "info": Path(info.stdout_log).read_text(),
                "fonts": Path(fonts.stdout_log).read_text(),
            }
    branch.report["documentation"]["languages"][language].update(
        status="built", validation=validations
    )


def export(branch):
    goal = branch.request.plan["spec"]["goal"]
    report = branch.report["documentation"]
    profile = branch.config["documentation"]
    if goal in {"doc-source", "translations", "translated-docs"}:
        source = paths(branch) / "written"
        shutil.copytree(source, branch.output / "documentation/source", symlinks=True)
        if goal == "translated-docs":
            for language in profile["languages"]:
                shutil.copytree(
                    paths(branch) / language / "source",
                    branch.output / "documentation" / language,
                    symlinks=True,
                )
        write_json(branch.output / "documentation/source-manifest.json", report)
        return
    entries = inventory(branch.output / "docs")["entries"]
    if (
        not entries
        or set(report["languages"]) != set(profile["languages"])
        or any(v["status"] != "built" for v in report["languages"].values())
    ):
        raise PreconditionError("documentation matrix is incomplete")
    document = {
        "api_version": "pg_maker/docs-artifact-manifest/v1",
        "context": context(branch),
        "source_hash": report["source_hash"],
        "transformed_hash": report["transformed_hash"],
        "profile": profile,
        "languages": report["languages"],
        "entries": entries,
        "tools": next(
            i["tools"]
            for i in branch.request.lock["spec"]["environments"]["spec"]["images"]
            if i["id"] == branch.variant["target"] + ".build"
        ),
        "image_id": branch.step["image_id"],
    }
    document["manifest_hash"] = content_hash(document)
    write_json(branch.output / "docs-manifest.json", document)
    report["manifest"] = "docs-manifest.json"
    if goal == "docs":
        archives = []
        for language in profile["languages"]:
            for fmt in profile["formats"]:
                output = branch.output / f"{branch.id}.{language}.{fmt}.tar.xz"
                with tarfile.open(output, "w:xz") as archive:
                    source = branch.output / "docs" / language / fmt
                    for path in sorted(source.rglob("*")):
                        info = archive.gettarinfo(
                            str(path), arcname=path.relative_to(source).as_posix()
                        )
                        info.uid = info.gid = info.mtime = 0
                        info.uname = info.gname = "root"
                        if path.is_file():
                            with path.open("rb") as stream:
                                archive.addfile(info, stream)
                        else:
                            archive.addfile(info)
                archives.append(
                    {
                        "path": output.name,
                        "sha256": file_hash(output),
                        "size_bytes": output.stat().st_size,
                        "role": "documentation",
                        "language": language,
                        "format": fmt,
                        "verification": {
                            "path": "docs-manifest.json",
                            "sha256": file_hash(branch.output / "docs-manifest.json"),
                        },
                    }
                )
        manifest = {
            "api_version": "pg_maker/artifact-manifest/v1",
            "artifacts": archives,
            "publication_eligible": False,
        }
        manifest["manifest_hash"] = content_hash(manifest)
        write_json(branch.output / "artifact-manifest.json", manifest)


def execute(branch, step):
    kind = step["kind"]
    if kind == "prepare_documentation":
        prepare(branch)
    elif kind == "export_documentation":
        export(branch)
    elif kind.startswith("translate_docs_"):
        translate(branch, step["parameters"]["language"], step["parameters"]["extract_only"])
    else:
        build(branch, step["parameters"]["language"])
