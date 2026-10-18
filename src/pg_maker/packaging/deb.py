"""Debian dependency discovery and package creation in the locked build image."""

from __future__ import annotations

import hashlib
import io
import os
import re
import tarfile
from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.packaging.service import maintainer_scripts
from pg_maker.packaging.spec import deb_control, field, package_spec
from pg_maker.packaging.tree import split, verify_tree
from pg_maker.storage import file_hash, read_json, write_json


def inspect_deb(path: Path) -> dict:
    """Inspect archive bytes without extracting or trusting dpkg's command exit alone."""
    members = {}
    with path.open("rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            raise PreconditionError("invalid DEB archive")
        while header := stream.read(60):
            if len(header) != 60 or header[58:] != b"\x60\n":
                raise PreconditionError("truncated DEB archive header")
            name, size = header[:16].decode("ascii").strip().rstrip("/"), int(header[48:58])
            if name in members or size < 0 or size > path.stat().st_size:
                raise PreconditionError("invalid DEB member")
            data = stream.read(size)
            if len(data) != size:
                raise PreconditionError("truncated DEB member")
            members[name] = data
            if size % 2:
                stream.read(1)
    if (
        set(members) != {"debian-binary", "control.tar.xz", "data.tar.xz"}
        or members["debian-binary"] != b"2.0\n"
    ):
        raise PreconditionError("unsupported DEB archive layout")
    controls, entries = {}, []
    for name in ("control.tar.xz", "data.tar.xz"):
        seen = set()
        with tarfile.open(fileobj=io.BytesIO(members[name]), mode="r:xz") as archive:
            for member in archive:
                relative = member.name.removeprefix("./").rstrip("/")
                if relative in {"", "."}:
                    continue
                from pg_maker.documents import relative_output

                if not relative_output(relative) or relative in seen:
                    raise PreconditionError("invalid or duplicate package path")
                seen.add(relative)
                if name == "control.tar.xz":
                    if not member.isfile() or "/" in relative:
                        raise PreconditionError("unexpected control archive member")
                    controls[relative] = archive.extractfile(member).read().decode("utf-8")
                    if relative in {"postinst", "prerm", "postrm"} and member.mode != 0o755:
                        raise PreconditionError("maintainer script is not executable")
                    continue
                if member.uid != 0 or member.gid != 0:
                    raise PreconditionError("packaged files must belong to root")
                item = {"path": relative, "mode": member.mode}
                if member.isdir():
                    item["type"] = "directory"
                elif member.issym():
                    item.update(type="symlink", target=member.linkname)
                elif member.isfile():
                    digest = hashlib.sha256()
                    with archive.extractfile(member) as incoming:
                        for block in iter(lambda: incoming.read(1048576), b""):
                            digest.update(block)
                    item.update(
                        type="file", size=member.size, sha256="sha256:" + digest.hexdigest()
                    )
                else:
                    raise PreconditionError("unsupported package file type")
                entries.append(item)
    metadata = {}
    for line in controls.get("control", "").splitlines():
        if line and not line[0].isspace():
            key, separator, value = line.partition(": ")
            if not separator or key in metadata:
                raise PreconditionError("invalid package control fields")
            metadata[key] = value
    return {"control": metadata, "scripts": controls, "entries": entries}


def declared_dependencies(actual, declared, service, internal):
    """ELF requirements must fit the prepared closure; service tools are explicit."""

    def names(values):
        return {
            alternative.strip().split()[0].split(":")[0]
            for value in values
            for alternative in value.split("|")
        }

    missing = names(actual) - names(declared) - names(internal)
    if missing:
        raise PreconditionError(
            "undeclared runtime dependencies", details={"missing": sorted(missing)}
        )
    # Preserve version constraints discovered by shlibdeps; no duplicate unversioned entries.
    used = names(actual)
    return sorted(set(actual) | {v for v in service if not names([v]) <= used}), sorted(
        names(declared) - used
    )


def verify_libpq_soname(sonames, abi_suffix):
    if sonames != {"libpq.so." + abi_suffix}:
        raise PreconditionError(
            "libpq SONAME differs from role abi_suffix",
            details={"sonames": sorted(sonames), "abi_suffix": abi_suffix},
        )


def dependencies(branch) -> dict:
    tree = read_json(branch.output / "install-tree.json")
    root = branch.root / "install-tree"
    verify_tree(root, tree)
    workspace = branch.root / "packaging"
    workspace.mkdir()
    packages = branch.variant["identity"]["packages"]
    split(root, tree, workspace / "debian", packages)
    branch.prepare()
    container = branch.container_root + "/packaging"
    (workspace / "debian/control").write_text(
        "Source: pg-maker-runtime\nSection: database\nPriority: optional\n"
        "Maintainer: "
        + field(branch.config["vendor"]["contact_email"])
        + "\n\n"
        + "\n\n".join(
            "Package: " + p["name"] + "\nArchitecture: any\nDescription: PostgreSQL runtime"
            for p in packages
        )
        + "\n"
    )
    files = [e for e in tree["entries"] if e["type"] == "file"]
    elfs = []
    for entry in files:
        with (root / entry["path"]).open("rb") as stream:
            if stream.read(4) == b"\x7fELF":
                elfs.append(entry)
    if not elfs:
        raise PreconditionError("runtime tree contains no ELF binaries")
    paths = [branch.container_root + "/install-tree/" + e["path"] for e in elfs]
    result = branch.command(["readelf", "--dynamic", "--wide", *paths])
    dynamic, current = {p: {"needed": [], "rpath": []} for p in paths}, paths[0]
    for line in Path(result.stdout_log).read_text().splitlines():
        if line.startswith("File: "):
            current = line[6:]
        value = re.search(r"\((NEEDED|SONAME|RPATH|RUNPATH)\).*\[(.*)\]", line)
        if value:
            kind, text = value.groups()
            if kind == "SONAME":
                dynamic[current]["soname"] = text
            else:
                dynamic[current]["needed" if kind == "NEEDED" else "rpath"].extend(
                    text.split(":") if kind != "NEEDED" else [text]
                )
    by_role = {p["role"]: p for p in packages}
    sonames, shlibs = {}, []
    for entry, path in zip(elfs, paths, strict=True):
        name = dynamic[path].get("soname")
        if name:
            if name in sonames:
                raise PreconditionError("duplicate runtime SONAME", details={"soname": name})
            sonames[name] = entry
            matched = re.fullmatch(r"(.*)\.so\.(.+)", name)
            if matched:
                package = by_role[entry["role"]]
                shlibs.append(
                    f"{matched[1]} {matched[2]} {package['name']} (= {package['deb_version']})"
                )
    (workspace / "debian/shlibs.local").write_text("\n".join(shlibs) + "\n")
    for entry, path in zip(elfs, paths, strict=True):
        info = dynamic[path]
        for needed in info["needed"]:
            if needed in sonames:
                parent = "/" + str(Path(sonames[needed]["path"]).parent)
                origin = "/" + str(Path(entry["path"]).parent)
                search = [os.path.normpath(p.replace("$ORIGIN", origin)) for p in info["rpath"]]
                if parent not in search:
                    raise PreconditionError(
                        "ELF lacks runtime search path for bundled library",
                        details={"path": entry["path"], "library": needed},
                    )
        if any("/work/" in p or "/tmp/" in p for p in info["rpath"]):
            raise PreconditionError("ELF runtime search path contains a build directory")
    result = {}
    target = next(t for t in branch.config["targets"] if t["id"] == branch.variant["target"])
    recipe = target["platform"]["spec"]
    verify_libpq_soname(
        {
            dynamic[p].get("soname")
            for e, p in zip(elfs, paths, strict=True)
            if e["role"] == "libpq" and dynamic[p].get("soname")
        },
        recipe["roles"]["libpq"]["abi_suffix"],
    )
    warnings = {}
    for package in packages:
        role = package["role"]
        binaries = [
            container + "/debian/" + package["name"] + "/" + e["path"]
            for e in elfs
            if e["role"] == role
        ]
        found = set()
        if binaries:
            command = branch.command(
                [
                    "dpkg-shlibdeps",
                    "-O",
                    "-x" + package["name"],
                    "-l" + branch.prefix + "/lib",
                    "-l" + branch.prefix + "/lib/postgresql",
                    *binaries,
                ],
                cwd=container,
            )
            output = Path(command.stdout_log).read_text()
            match = re.search(r"^shlibs:Depends=(.*)$", output, re.M)
            if match:
                found.update(v.strip() for v in match[1].split(",") if v.strip())
        declared = set(recipe["runtime_dependencies"].get(role, []))
        for option, enabled in branch.variant["build"]["options"].items():
            if enabled:
                declared.update(recipe.get("option_runtime_dependencies", {}).get(option, []))
        service = recipe.get("service_dependencies", {}).get(role, [])
        if role == "server":
            declared.update(
                c
                for component in branch.feature["spec"]["components"]
                for c in component["spec"]["runtime_dependencies"]
            )
        found, unused = declared_dependencies(
            found, declared, service, [p["name"] for p in packages]
        )
        found = set(found)
        if unused:
            warnings[role] = unused
        for required_role in {"server": ("client", "libpq"), "client": ("libpq",)}.get(role, ()):
            required = by_role[required_role]
            found.add(required["name"] + " (= " + required["deb_version"] + ")")
        result[role] = sorted(found)
    write_json(
        branch.output / "runtime-dependencies.json",
        {"packages": result, "elf": dynamic, "unused_declared": warnings},
    )
    if warnings:
        branch.report.setdefault("warnings", {})["unused_runtime_dependencies"] = warnings
    return result


def package(branch) -> list[dict]:
    document = read_json(branch.output / "install-tree.json")
    verify_tree(branch.root / "install-tree", document)
    dependencies_by_role = read_json(branch.output / "runtime-dependencies.json")["packages"]
    packages = branch.variant["identity"]["packages"]
    artifacts = []
    destination = branch.output / "packages"
    destination.mkdir()
    for package in packages:
        role = package["role"]
        staging = branch.root / "packaging/debian" / package["name"]
        control = staging / "DEBIAN"
        control.mkdir()
        spec = package_spec(
            branch.config, branch.variant, package, dependencies_by_role[role], document
        )
        write_json(branch.output / "package-specs" / (role + ".json"), spec)
        rendered, values = deb_control(spec)
        (control / "control").write_text(rendered)
        (control / "control").chmod(0o644)
        if role == "server":
            for name, script in maintainer_scripts(branch.config, branch.variant).items():
                path = control / name
                path.write_text(script)
                path.chmod(0o755)
        filename = package["filename"]
        if branch.branch == "baseline":
            filename = filename.removesuffix(".deb") + ".baseline.deb"
        temporary = destination / (filename + ".partial")
        branch.command(
            [
                "dpkg-deb",
                "--root-owner-group",
                "--uniform-compression",
                "-Zxz",
                "-z6",
                "--threads-max=" + str(branch.variant["build"]["jobs"]),
                "--build",
                branch.container_root + "/packaging/debian/" + package["name"],
                "/work/output/" + branch.id + "/packages/" + temporary.name,
            ],
            env={"SOURCE_DATE_EPOCH": "1"},
        )
        inspected = inspect_deb(temporary)
        if any(inspected["control"].get(k) != v for k, v in values.items() if v):
            raise PreconditionError("DEB metadata differs from planned package")
        expected = {
            e["path"]: {
                k: e[k] for k in ("path", "type", "mode", "sha256", "target", "size") if k in e
            }
            for e in document["entries"]
            if e["role"] == role
        }
        # Symlink lstat size in InstallTree is not a tar payload size.
        for entry in expected.values():
            if entry["type"] == "symlink":
                entry.pop("size", None)
        actual = {e["path"]: e for e in inspected["entries"] if e["type"] != "directory"}
        if actual != expected:
            raise PreconditionError("DEB payload differs from InstallTree")
        if role != "server" and set(inspected["scripts"]) & {"postinst", "prerm", "postrm"}:
            raise PreconditionError("client/libpq packages must not manage a cluster")
        final = destination / filename
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        artifacts.append(
            {
                **package,
                "path": "packages/" + filename,
                "sha256": file_hash(final),
                "size_bytes": final.stat().st_size,
                "dependencies": dependencies_by_role[role],
            }
        )
    verify_tree(branch.root / "install-tree", document)
    write_json(branch.output / "packages.json", artifacts)
    branch.report["packages"] = artifacts
    return artifacts
