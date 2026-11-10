"""Publication gate over successful operations and hash-bound package test evidence."""

import re
from pathlib import PurePosixPath

from pg_maker.artifacts.manifest import verify_manifest
from pg_maker.documents import relative_output, validate_document
from pg_maker.errors import PreconditionError
from pg_maker.extensions.verification import verify_extension_output
from pg_maker.storage import content_hash, file_hash, read_json


def plain(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PreconditionError("publication source must not traverse symlinks")
    return path


def source_state(source):
    plain(source)
    state = read_json(plain(source / "state.json"))
    payload = read_json(plain(source / "input.json"))
    if state["input_hash"] != content_hash(payload) or state["operation_kind"] not in {
        "build",
        "extension_build",
    }:
        raise PreconditionError(
            "publication requires a database or extension build operation/export"
        )
    return state


def verify_export(source, state):
    """Check output completeness and seal anchors without reading exported SDKs."""
    manifest = read_json(plain(source / "export-manifest.json"))
    inventory = manifest.get("inventory", {})
    if (
        not isinstance(inventory, dict)
        or not isinstance(inventory.get("entries"), list)
        or manifest.get("tree_hash") != content_hash(inventory)
        or manifest.get("operation_id") != state.get("operation_id")
    ):
        raise PreconditionError("invalid publication export inventory")
    selected = {}
    for entry in inventory["entries"]:
        name = entry.get("path")
        if not isinstance(name, str) or not relative_output(name):
            raise PreconditionError("invalid publication export path")
        if name in {"input.json", "state.json", "output"} or name.startswith("output/"):
            if name in selected:
                raise PreconditionError("duplicate publication export path")
            selected[name] = entry
    if not {"input.json", "state.json", "output"} <= selected.keys():
        raise PreconditionError("publication export inventory lacks build outputs")
    actual = {"input.json", "state.json", "output"} | {
        p.relative_to(source).as_posix() for p in (source / "output").rglob("*")
    }
    if actual != selected.keys():
        raise PreconditionError("publication export output inventory differs")
    for name, entry in selected.items():
        path = plain(source / name)
        kind = entry.get("type")
        if not ((kind == "file" and path.is_file()) or (kind == "directory" and path.is_dir())):
            raise PreconditionError("publication export entry missing or changed")
        # Package/evidence bytes are checked by their sealed manifests below.
        if name in {"input.json", "state.json"} or path.name in {
            "artifact-manifest.json",
            "extension-manifest.json",
        }:
            if kind != "file" or file_hash(path) != entry.get("sha256"):
                raise PreconditionError("publication export seal changed")


def packages(source):
    state = source_state(source)
    if state["status"] != "succeeded" or state["resources"]:
        raise PreconditionError("publication requires a successful build with released resources")
    exported = (source / "export-manifest.json").exists()
    if exported:
        verify_export(source, state)
    root = plain(source / ("output" if exported else "workspace/output"))
    records = []

    def add(directory, record):
        relative = record["path"]
        if not relative_output(relative):
            raise PreconditionError("invalid package reference")
        path = plain(directory / relative)
        if path.suffix not in {".deb", ".rpm"}:
            return
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+~:-]*\.(deb|rpm)", path.name):
            raise PreconditionError("unsupported native package filename")
        if not path.is_file() or file_hash(path) != record["sha256"]:
            raise PreconditionError("package bytes changed after testing")
        records.append(
            {
                "path": str(path),
                "filename": path.name,
                "format": path.suffix[1:],
                "sha256": record["sha256"],
                "size_bytes": path.stat().st_size,
                "name": record["name"],
                "version": record.get("version")
                or (
                    record["deb_version"]
                    if path.suffix == ".deb"
                    else record["rpm_version"] + "-" + record["rpm_release"]
                ),
                "architecture": record.get("native_arch") or record["architecture"],
            }
        )

    if state["operation_kind"] == "extension_build":
        verify_extension_output(root)
        report = read_json(root / "report.json")
        tests = read_json(root / "package-tests/package-tests.json")
        if (
            report["qualification"] != "extension-package-verified"
            or report["package_tests"] != tests
            or not tests.get("passed")
            or not tests.get("installed_inventory")
        ):
            raise PreconditionError("extension publication requires clean-container package tests")
        for record in report["packages"]:
            add(
                root / "packages",
                {**record, "architecture": report["target"]["server_package"]["architecture"]},
            )
        migration = root / "target-packages/migration.json"
        if migration.is_file():
            for record in read_json(migration):
                add(
                    root,
                    {
                        **record,
                        "path": "target-packages/" + record["filename"],
                        "architecture": report["target"]["server_package"]["architecture"],
                    },
                )
    else:
        manifests = sorted(root.glob("*/artifact-manifest.json"))
        payload = read_json(source / "input.json")
        expected = {
            (PurePosixPath(output["path"]).parent / "artifact-manifest.json").as_posix()
            for step in payload["plan"]["spec"]["steps"]
            if step["kind"] == "export"
            for output in step["outputs"]
            if output["root"] == "output"
        }
        if not expected or expected != {p.relative_to(root).as_posix() for p in manifests}:
            raise PreconditionError("package manifests do not cover the build plan")
        for path in manifests:
            manifest = read_json(plain(path))
            verify_manifest(path.parent, manifest)
            verification = read_json(path.parent / "verification.json")
            evidence = {r["path"] for r in verification["evidence"]}
            if "package-tests.json" not in evidence:
                raise PreconditionError("manifest lacks sealed package installation evidence")
            tests = read_json(path.parent / "package-tests.json")
            if (
                not tests.get("passed")
                or not tests.get("run_database")
                or not tests.get("sets")
                or not all(t.get("passed") for t in tests["sets"])
            ):
                raise PreconditionError(
                    "database publication requires clean-container database tests"
                )
            for test in tests["sets"]:
                if (
                    test["path"] not in evidence
                    or file_hash(path.parent / test["path"]) != test["sha256"]
                ):
                    raise PreconditionError("package test result is not bound to verified evidence")
            for record in manifest["artifacts"]:
                if record["role"] != "baseline-package":
                    add(path.parent, record)
    if not records:
        raise PreconditionError("no tested DEB/RPM artifacts to publish")
    unique = {}
    for record in records:
        identity = (record["format"], record["name"], record["version"], record["architecture"])
        if identity in unique and unique[identity]["sha256"] != record["sha256"]:
            raise PreconditionError("conflicting bytes for one native package identity")
        unique.setdefault(identity, record)
    return sorted(unique.values(), key=lambda r: r["path"])


def plan(source, config):
    from pg_maker.publishing.config import validate

    validate(config)
    artifacts = packages(source)
    destinations = []
    covered = set()
    for receiver in config["repositories"]:
        selected = [p for p in artifacts if p["format"] in receiver["formats"]]
        if not selected:
            raise PreconditionError(
                "receiver has no matching packages", details={"receiver": receiver["id"]}
            )
        seen = {}
        for package in selected:
            name = package["filename"]
            if name in seen and seen[name] != package["sha256"]:
                raise PreconditionError("receiver filename collision")
            seen[name] = package["sha256"]
            covered.add(package["path"])
            destinations.append({"receiver": receiver["id"], "package": package})
    if covered != {p["path"] for p in artifacts}:
        raise PreconditionError("some packages have no configured receiver")
    result = {
        "api_version": "pg_maker/publication-plan/v1",
        "source": str(source),
        "source_input_hash": source_state(source)["input_hash"],
        "config_hash": content_hash(config),
        "receivers": config["repositories"],
        "destinations": destinations,
    }
    result["plan_hash"] = content_hash(result)
    validate_document(result, "publication-plan")
    return result
