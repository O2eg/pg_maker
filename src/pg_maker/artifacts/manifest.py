"""Portable artifact manifests and immutable verification records."""

from __future__ import annotations

import copy
import html
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError
from pg_maker.packaging.service import settings
from pg_maker.sources.store import InputStore, inventory
from pg_maker.storage import (
    content_hash,
    ensure_directory,
    file_hash,
    read_json,
    sync_directory,
    write_json,
    write_text,
)


def finish(branch):
    qualified = branch.report["qualification"]
    if branch.request.plan["spec"]["goal"] == "verified":
        results = branch.report.get("package_tests", {})
        if (
            qualified != "source-verified"
            or not results.get("passed")
            or not results.get("run_database")
        ):
            raise PreconditionError("verified goal lacks source/package evidence")
        qualified = "package-verified"
    # Eligibility is granted only after the entire matrix (including baseline) succeeds.
    eligible = False
    evidence = []
    for path in sorted(branch.output.rglob("*.json")):
        if path.name in {"report.json", "artifact-manifest.json", "verification.json"}:
            continue
        if "commands" not in path.parts:
            evidence.append(
                {
                    "path": path.relative_to(branch.output).as_posix(),
                    "sha256": file_hash(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    verification = {
        "api_version": "pg_maker/verification/v1",
        "qualification": qualified,
        "run_id": branch.context.directory.name,
        "attempt": branch.context.state["attempt"],
        "plan_hash": branch.request.plan["plan_hash"],
        "lock_hash": branch.request.lock["lock_hash"],
        "db_tests": branch.report["db_tests"],
        "evidence": evidence,
        "service_manager": "emulated" if qualified == "package-verified" else "not-tested",
    }
    verification["verification_hash"] = content_hash(verification)
    write_json(branch.output / "verification.json", verification)
    target = next(t for t in branch.config["targets"] if t["id"] == branch.variant["target"])
    common = {
        "target_id": target["id"],
        "os": target["platform"]["spec"]["os"],
        "architecture": target["architecture"],
        "feature": branch.feature["metadata"]["id"],
        "vendor": branch.config["vendor"]["slug"],
        "build_mode": branch.variant["build"]["mode"],
        "postgresql_version": branch.config["postgresql"]["version"],
        "product_version": branch.variant["identity"]["product_version"],
        "source_hash": branch.report["source_hash"],
        "install_tree_hash": branch.report["install_tree"]["tree_hash"],
        "producer": {
            "run_id": branch.context.directory.name,
            "lock_hash": branch.request.lock["lock_hash"],
            "plan_hash": branch.request.plan["plan_hash"],
        },
        "build_producer": branch.report.get("cache_producer"),
        "build_producer_scope": "core-and-bundled-contrib",
        "component_producers": {
            c["id"]: c.get(
                "cache_producer",
                {
                    "run_id": branch.context.directory.name,
                    "lock_hash": branch.request.lock["lock_hash"],
                },
            )
            for c in branch.report.get("components", [])
        },
        "qualification": qualified,
        "verification": {
            "path": "verification.json",
            "sha256": file_hash(branch.output / "verification.json"),
        },
    }
    artifacts = [
        {
            **common,
            **package,
            "id": package["name"],
            "media_type": "application/vnd.debian.binary-package",
            "package_role": package["role"],
            "role": "baseline-package" if branch.branch == "baseline" else package["role"],
        }
        for package in branch.report["packages"]
    ]
    if branch.config.get("documentation", {}).get("enabled", False):
        docs = read_json(branch.output / "docs-manifest.json")
        if branch.request.plan["spec"]["goal"] == "verified" and any(
            not value["quality"]["complete"] for value in docs["languages"].values()
        ):
            raise PreconditionError("incomplete documentation cannot qualify a package")
        for artifact in artifacts:
            artifact["documentation"] = {
                "path": "docs-manifest.json",
                "sha256": file_hash(branch.output / "docs-manifest.json"),
                "languages": docs["profile"]["languages"],
                "formats": docs["profile"]["formats"],
                "backend": docs["profile"]["html_backend"],
                "manifest_hash": docs["manifest_hash"],
            }
    manifest = {
        "api_version": "pg_maker/artifact-manifest/v1",
        "artifacts": artifacts,
        "publication_eligible": eligible,
    }
    manifest["manifest_hash"] = content_hash(manifest)
    verify_manifest(branch.output, manifest)
    write_json(branch.output / "artifact-manifest.json", manifest)
    for name, value in (("lock", branch.request.lock), ("plan", branch.request.plan)):
        document = copy.deepcopy(value)
        document["transport"] = {}
        write_json(branch.output / (name + ".json"), document)
    s = settings(branch.config, branch.variant)
    commands = " ".join("./" + p["path"] for p in branch.report["packages"])
    write_text(
        branch.output / "INSTALL.txt",
        "Install with the distribution package manager:\n"
        "apt-get install " + commands + "\n\n"
        "Installation does not initialize or start a database.\n"
        f"runuser -u {s['user']} -- {s['prefix']}/bin/initdb -D {s['data']}\n"
        f"systemctl start {s['name']}.service\n"
        "Configuration and file logs belong inside PGDATA. Remove/purge preserves PGDATA.\n"
        f"Client programs: {s['prefix']}/bin\n"
        f"Qualification: {qualified}; DB tests: {branch.report['db_tests']}.\n",
    )
    branch.report.update(
        qualification=qualified,
        publication_eligible=eligible,
        artifact_manifest="artifact-manifest.json",
        verification="verification.json",
    )
    render_report(branch)


def render_report(branch):
    write_text(
        branch.output / "report.html",
        '<!doctype html><meta charset="utf-8"><title>pg_maker report</title>'
        "<h1>pg_maker</h1><pre>"
        + html.escape(json.dumps(branch.report, indent=2, ensure_ascii=False))
        + "</pre>",
    )


def complete_matrix(reports: list[Path]) -> None:
    """Preflight all branches before granting publication eligibility to any candidate."""
    checked = []
    for path in reports:
        report = read_json(path)
        if report.get("status") != "succeeded" or report.get("qualification") != "package-verified":
            raise PreconditionError("publication eligibility requires every verified branch")
        manifest = read_json(path.parent / "artifact-manifest.json")
        verify_manifest(path.parent, manifest)
        checked.append((path, report, manifest))
    for path, report, manifest in checked:
        if report["branch"] != "candidate":
            continue
        manifest["publication_eligible"] = True
        manifest["manifest_hash"] = content_hash(
            {k: v for k, v in manifest.items() if k != "manifest_hash"}
        )
        write_json(path.parent / "artifact-manifest.json", manifest)
        report["publication_eligible"] = True
        write_json(path, report)
        render_report(SimpleNamespace(output=path.parent, report=report))


def verify_manifest(root: Path, manifest: dict):
    core = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    if manifest.get("manifest_hash") != content_hash(core):
        raise PreconditionError("artifact manifest hash changed")
    for artifact in manifest["artifacts"]:
        for reference in (artifact, artifact["verification"]):
            path = reference["path"]
            if not relative_output(path):
                raise PreconditionError("artifact reference must be relative")
            file = root / path
            if any(p.is_symlink() for p in (file, *file.parents)) or not file.is_file():
                raise PreconditionError("artifact is missing or traverses a symlink")
            if file_hash(file) != reference["sha256"]:
                raise PreconditionError("artifact hash changed", details={"path": path})
            if "size_bytes" in reference and file.stat().st_size != reference["size_bytes"]:
                raise PreconditionError("artifact size changed")
    verification_path = root / "verification.json"
    if verification_path.is_file():
        value = read_json(verification_path)
        if value["verification_hash"] != content_hash(
            {k: v for k, v in value.items() if k != "verification_hash"}
        ):
            raise PreconditionError("verification record hash changed")
        for evidence in value["evidence"]:
            file = root / evidence["path"]
            if (
                not relative_output(evidence["path"])
                or any(p.is_symlink() for p in (file, *file.parents))
                or not file.is_file()
                or file_hash(file) != evidence["sha256"]
            ):
                raise PreconditionError("verification evidence changed")


def export_operation(directory: Path, destination: Path, *, include_inputs=False) -> dict:
    if destination.exists() or destination.is_symlink():
        raise PreconditionError("export destination already exists")
    ensure_directory(destination.parent)
    output = directory / "workspace/output"
    for path in output.glob("*/artifact-manifest.json"):
        verify_manifest(path.parent, read_json(path))
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".pg-maker-export-") as name:
        staging = Path(name) / "result"
        staging.mkdir()
        if output.is_dir():
            shutil.copytree(output, staging / "output", symlinks=True)
        for name in ("attempts",):
            if (directory / name).is_dir():
                shutil.copytree(
                    directory / name,
                    staging / name,
                    symlinks=True,
                    ignore=shutil.ignore_patterns("workspace", "previous"),
                )
        for name in ("input.json", "state.json", "events.jsonl", "worker.log"):
            if (directory / name).is_file():
                shutil.copy2(directory / name, staging / name)
        request = read_json(directory / "input.json")
        state = read_json(directory / "state.json")
        if request["operation_kind"] in {
            "prepare_environments",
            "resolve_inputs",
            "runtime_image",
        } and state.get("result"):
            produced = Path(state["result"]["document"])
            if file_hash(produced) != state["result"]["sha256"]:
                raise PreconditionError("prepared output changed before export")
            shutil.copyfile(produced, staging / "prepared-document.json")
        if include_inputs and request["operation_kind"] == "build":
            store = InputStore(Path(request["input_store"]))
            config = request["lock"]["spec"]["config"]
            trees = {config["source"]["source_hash"]}
            trees.update(
                c["spec"]["source"]["source_hash"]
                for f in config["features"]
                for c in f["spec"]["components"]
                if "source" in c["spec"]
            )
            destination_store = InputStore(staging / "inputs")
            for digest in trees:
                destination_store.put_tree(store.tree(digest))
            for digest in request["lock"]["spec"]["blobs"].values():
                destination_store.put_blob(store.blob(digest), digest)
        for path in (staging / "output").glob("*/artifact-manifest.json"):
            verify_manifest(path.parent, read_json(path))
        contents = inventory(staging)
        write_json(
            staging / "export-manifest.json",
            {
                "api_version": "pg_maker/export/v1",
                "operation_id": directory.name,
                "includes_inputs": include_inputs,
                "inventory": contents,
                "tree_hash": content_hash(contents),
            },
        )
        for entry in contents["entries"]:
            if entry["type"] == "file":
                with (staging / entry["path"]).open("rb") as stream:
                    os.fsync(stream.fileno())
        directories = [staging / e["path"] for e in contents["entries"] if e["type"] == "directory"]
        for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            sync_directory(path)
        sync_directory(staging)
        os.replace(staging, destination)
        sync_directory(destination.parent)
    return {"directory": str(destination), "tree_hash": content_hash(contents)}
