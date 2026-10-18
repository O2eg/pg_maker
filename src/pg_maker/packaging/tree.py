"""Immutable runtime tree and explicit, nonoverlapping package ownership."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import tempfile
from pathlib import Path

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, ensure_directory, read_json, write_json

CLIENT_PROGRAMS = frozenset(
    "psql pg_dump pg_dumpall pg_restore pg_basebackup pg_receivewal pg_recvlogical "
    "pg_verifybackup pg_amcheck pg_isready clusterdb createdb createuser dropdb dropuser "
    "reindexdb vacuumdb pg_config".split()
)


def excluded(path: str) -> str | None:
    parts, name = Path(path).parts, Path(path).name
    if parts[0] == "include" or "pgxs" in parts or "pkgconfig" in parts:
        return "development"
    if name in {"ecpg", "ecpg.mo"} or name.startswith(("libecpg", "libpgtypes", "ecpg-")):
        return "ecpg"
    if name.endswith((".a", ".la")) or name == "libpq.so":
        return "development"
    if path.startswith(("share/doc/", "share/man/")):
        return "documentation-disabled"
    return None


def owner(path: str) -> str:
    name = Path(path).name
    if name.startswith("libpq.so.") or (name.startswith("libpq") and name.endswith(".mo")):
        return "libpq"
    if path.startswith("bin/") and name in CLIENT_PROGRAMS:
        return "client"
    if name == "psqlrc.sample":
        return "client"
    if name.endswith(".mo") and any(
        name == program + ".mo" or name.startswith(program + "-") for program in CLIENT_PROGRAMS
    ):
        return "client"
    if "/man" in path and name.split(".")[0] in CLIENT_PROGRAMS:
        return "client"
    return "server"


def matches(path: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern) or (pattern.endswith("/**") and path == pattern[:-3])


def seal_tree(root: Path, roles: dict[str, str]) -> dict:
    entries = inventory(root)["entries"]
    for item in entries:
        path = root / item["path"]
        item.update(
            owner="root",
            group="root",
            size=0 if item["type"] == "directory" else path.lstat().st_size,
        )
        if item["type"] == "directory":
            item["role"] = "shared-directory"
        else:
            if item["path"] not in roles:
                raise PreconditionError("runtime file has no package owner", details=item)
            item["role"] = roles[item["path"]]
        if item["mode"] & 0o6000:
            raise ValidationError("setuid/setgid runtime files are not supported", details=item)
    core = {"api_version": "pg_maker/install-tree/v1", "entries": entries}
    return {**core, "tree_hash": content_hash(core)}


def verify_tree(root: Path, document: dict) -> None:
    roles = {
        item["path"]: item["role"] for item in document["entries"] if item["type"] != "directory"
    }
    if seal_tree(root, roles) != document:
        raise PreconditionError("InstallTree content or metadata changed")


def collect(branch) -> dict:
    from pg_maker.packaging.service import service_files

    prefix = branch.root / "prefix"
    source = inventory(prefix)
    core = read_json(branch.output / "core-files.json")
    core_paths = {item["path"] for item in core["entries"] if item["type"] != "directory"}
    files = {item["path"]: item for item in source["entries"] if item["type"] != "directory"}
    for entry in core["entries"]:
        if entry["type"] != "directory" and not excluded(entry["path"]):
            if files.get(entry["path"]) != entry:
                raise PreconditionError(
                    "component install changed a core runtime file", details={"path": entry["path"]}
                )
    branch.prepare()
    keys = (
        "bindir",
        "libdir",
        "pkglibdir",
        "sharedir",
        "sysconfdir",
        "docdir",
        "includedir",
        "pgxs",
    )
    result = branch.command([branch.prefix + "/bin/pg_config", *("--" + k for k in keys)])
    values = Path(result.stdout_log).read_text().splitlines()
    if len(values) != len(keys) or any(
        not value.startswith(branch.prefix + "/") or os.path.normpath(value) != value
        for value in values
    ):
        raise PreconditionError("pg_config layout differs from the private runtime prefix")
    version = branch.command([branch.prefix + "/bin/pg_config", "--version"])
    if not re.search(
        r"\b" + re.escape(branch.config["postgresql"]["version"]) + r"(?![0-9.])",
        Path(version.stdout_log).read_text(),
    ):
        raise PreconditionError("installed pg_config version differs from the lock")
    write_json(branch.output / "build-layout.json", dict(zip(keys, values, strict=True)))
    selected = set(core_paths)
    for component in branch.feature["spec"]["components"]:
        for pattern in component["spec"]["runtime_files"]:
            if not relative_output(pattern):
                raise ValidationError("component runtime pattern must be relative")
            found = {p for p in files if matches(p, pattern) and not excluded(p)}
            if not found:
                raise PreconditionError(
                    "component runtime pattern matched no deliverable files",
                    details={"component": component["metadata"]["id"], "pattern": pattern},
                )
            selected.update(found)
    destination = branch.root / "install-tree"
    if destination.exists():
        raise PreconditionError("InstallTree destination already exists")
    omissions, roles = [], {}
    ensure_directory(destination.parent)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".install-tree-") as name:
        staging = Path(name) / "root"
        staging.mkdir()
        runtime = branch.prefix.lstrip("/")
        for path, item in files.items():
            reason = excluded(path) or (None if path in selected else "unselected-component")
            if reason:
                omissions.append({"path": path, "reason": reason})
                continue
            target = staging / runtime / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if item["type"] == "symlink":
                target.symlink_to(item["target"])
            else:
                shutil.copy2(prefix / path, target, follow_symlinks=False)
            roles[runtime + "/" + path] = owner(path)
        required = ("bin/postgres", "bin/psql", "bin/pg_config")
        if any(runtime + "/" + p not in roles for p in required) or not any(
            role == "libpq" for role in roles.values()
        ):
            raise PreconditionError("runtime tree is missing server/client/libpq outputs")
        for path, (text, mode) in service_files(branch.config, branch.variant).items():
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            target.chmod(mode)
            roles[path] = "server"
        if branch.config["documentation"]["enabled"]:
            from pg_maker.documentation.install import collect_docs

            collect_docs(branch, staging, roles)
        license_path = branch.source / "COPYRIGHT"
        if not license_path.is_file():
            raise PreconditionError("PostgreSQL COPYRIGHT is missing")
        for package in branch.variant["identity"]["packages"]:
            path = "usr/share/doc/" + package["name"] + "/copyright"
            target = staging / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(license_path, target)
            target.chmod(0o644)
            roles[path] = package["role"]
        server = next(
            p["name"] for p in branch.variant["identity"]["packages"] if p["role"] == "server"
        )
        for component in branch.feature["spec"]["components"]:
            spec, identifier = component["spec"], component["metadata"]["id"]
            component_source = (
                branch.inputs.tree(spec["source"]["source_hash"])
                if "source" in spec
                else branch.source / spec.get("source_directory", "contrib/" + identifier)
            )
            for license_name in ("LICENSE", "COPYRIGHT", "COPYING", "LICENSE.md", "LICENSE.txt"):
                source_license = component_source / license_name
                if source_license.is_file() and not source_license.is_symlink():
                    path = f"usr/share/doc/{server}/components/{identifier}/{license_name}"
                    target = staging / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_license, target)
                    target.chmod(0o644)
                    roles[path] = "server"
        # Every symlink must resolve to a delivered path, not a discarded development file.
        for item in inventory(staging)["entries"]:
            if item["type"] == "directory":
                (staging / item["path"]).chmod(0o755)
            if item["type"] == "symlink" and not (staging / item["path"]).exists():
                raise PreconditionError("runtime symlink has no delivered target", details=item)
        document = seal_tree(staging, roles)
        os.replace(staging, destination)
    write_json(branch.output / "install-tree.json", document)
    write_json(branch.output / "excluded-files.json", omissions)
    branch.report["install_tree"] = {"tree_hash": document["tree_hash"], "files": len(roles)}
    return document


def split(root: Path, document: dict, destination: Path, packages: list[dict]) -> dict[str, Path]:
    verify_tree(root, document)
    result = {}
    for package in packages:
        role = package["role"]
        target = destination / package["name"]
        target.mkdir(parents=True)
        for entry in document["entries"]:
            if entry["role"] != role:
                continue
            source, output = root / entry["path"], target / entry["path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            if entry["type"] == "symlink":
                output.symlink_to(entry["target"])
            else:
                shutil.copy2(source, output, follow_symlinks=False)
        result[role] = target
        for directory in (
            target,
            *(p for p in target.rglob("*") if p.is_dir() and not p.is_symlink()),
        ):
            directory.chmod(0o755)
    return result
