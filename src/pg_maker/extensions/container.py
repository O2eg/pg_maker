"""Stdlib-only container helpers; all subprocesses use argv and bounded timeouts.

This file is copied into owned containers. It does not import the host installation.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def run(argv, *, check=True, cwd=None, env=None, user=None, timeout=3600):
    if user:
        argv = ["runuser", "-u", user, "--", *argv]
    started = time.monotonic()
    print(
        json.dumps({"argv": argv, "cwd": str(cwd) if cwd else None, "event": "started"}), flush=True
    )
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    captured = [[], []]

    def pump(pipe, output, fragments):
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with pipe:
            while block := pipe.read1(65536):
                text = decoder.decode(block)
                fragments.append(text)
                output.write(text)
                output.flush()
            text = decoder.decode(b"", final=True)
            fragments.append(text)
            output.write(text)
            output.flush()

    threads = [
        threading.Thread(target=pump, args=(pipe, output, fragments), daemon=True)
        for pipe, output, fragments in zip(
            (process.stdout, process.stderr), (sys.stdout, sys.stderr), captured, strict=True
        )
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            # A descendant must not retain our pipes after its command has ended.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for thread in threads:
                thread.join(timeout=5)
            if any(thread.is_alive() for thread in threads):
                raise RuntimeError("command output pipes did not close")
    result = subprocess.CompletedProcess(
        argv, process.returncode, "".join(captured[0]), "".join(captured[1])
    )
    print(
        json.dumps(
            {
                "argv": argv,
                "cwd": str(cwd) if cwd else None,
                "returncode": result.returncode,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "timed_out": timed_out,
            }
        ),
        flush=True,
    )
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout, output=result.stdout, stderr=result.stderr)
    if check and result.returncode:
        raise RuntimeError("command failed: " + repr(argv))
    return result


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return "sha256:" + value.hexdigest()


def apt(*args, **kwargs):
    return run(
        ["apt-get", "-y", "--no-install-recommends", *args],
        env={"DEBIAN_FRONTEND": "noninteractive"},
        **kwargs,
    )


def migration_modules(config):
    """The selected module set changes the prepared SDK and the resulting DEBs."""
    if not config["target"].get("split_bundled"):
        return []
    return sorted(
        {e["spec"]["sql_extension"] for e in config["extensions"]} & {"pg_stat_kcache", "pg_repack"}
    )


def migration_hash(job, packages):
    identity = {
        "revision": 2,
        "modules": migration_modules(job["config"]),
        "inputs": sorted(digest(package) for package in packages),
    }
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def split_database(job, packages):
    """Opt-in migration of known bundled modules; never edit input DEBs."""
    target = job["config"]["target"]
    if not target.get("split_bundled"):
        return packages
    if target["format"] != "deb" or target["provider"] != "profile":
        raise RuntimeError("bundled-file migration currently requires YC profile DEBs")
    names = migration_modules(job["config"])
    if not names:
        return packages
    fingerprint = migration_hash(job, packages)
    prefix = "opt/postgresql-yc-clone/" + str(target["major"])
    destination = Path("/work/output/target-packages")
    destination.mkdir(parents=True, exist_ok=True)
    records, produced = [], []
    for index, package in enumerate(packages):
        root = Path("/tmp/extension-target-split") / str(index)
        if root.exists():
            shutil.rmtree(root)
        root.parent.mkdir(parents=True, exist_ok=True)
        run(["dpkg-deb", "--raw-extract", str(package), str(root)])
        original_files = {
            f["path"]: f for f in file_inventory(root) if not f["path"].startswith("DEBIAN/")
        }
        control = root / "DEBIAN/control"
        text = control.read_text()
        old = re.search(r"^Version: (.+)$", text, re.M)[1]
        name = re.search(r"^Package: (.+)$", text, re.M)[1]
        arch = re.search(r"^Architecture: (.+)$", text, re.M)[1]
        new = old + "+extensions2." + fingerprint[7:27]
        if name not in {
            target["server_package"],
            f"postgresql-client-{target['major']}-yc-clone",
            "libpq5-yc-clone",
        }:
            raise RuntimeError("migration accepts only the three matching YC database packages")
        removed = []
        for extension in sorted(names):
            patterns = [
                prefix + "/bin/" + extension,
                prefix + "/lib/postgresql/" + extension + ".so",
                prefix + "/share/postgresql/extension/" + extension + ".control",
                prefix + "/share/postgresql/extension/" + extension + "--*.sql",
            ]
            for pattern in patterns:
                for path in root.glob(pattern):
                    removed.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "sha256": digest(path),
                            "extension": extension,
                        }
                    )
                    path.unlink()
        control.write_text(text.replace(old, new))
        for metadata in ("shlibs", "symbols"):
            path = root / "DEBIAN" / metadata
            if path.is_file():
                path.write_text(path.read_text().replace(old, new))
        md5 = root / "DEBIAN/md5sums"
        if md5.is_file():
            md5.write_text(
                "".join(
                    hashlib.md5(p.read_bytes()).hexdigest()
                    + "  "
                    + p.relative_to(root).as_posix()
                    + "\n"
                    for p in sorted(root.rglob("*"))
                    if p.is_file() and not p.is_symlink() and not p.is_relative_to(root / "DEBIAN")
                )
            )
        preserved = {
            f["path"]: f for f in file_inventory(root) if not f["path"].startswith("DEBIAN/")
        }
        removed_paths = {f["path"] for f in removed}
        if preserved != {p: f for p, f in original_files.items() if p not in removed_paths}:
            raise RuntimeError("database migration altered unrelated payload files")
        output = destination / f"{name}_{new}_{arch}.deb"
        run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz", str(root), str(output)])
        records.append(
            {
                "name": name,
                "old_version": old,
                "version": new,
                "original_sha256": digest(package),
                "migration_hash": fingerprint,
                "sha256": digest(output),
                "filename": output.name,
                "removed": removed,
                "preserved_files": len(preserved),
                "preserved_payload_sha256": "sha256:"
                + hashlib.sha256(json.dumps(preserved, sort_keys=True).encode()).hexdigest(),
            }
        )
        produced.append(output)
        shutil.rmtree(root)
    for extension in names:
        if not any(f["extension"] == extension for r in records for f in r["removed"]):
            raise RuntimeError("selected migration module is absent from target packages")
    write(destination / "migration.json", records)
    return produced


def provision(job, *, build):
    target = job["config"]["target"]
    deb = target["format"] == "deb"
    local = sorted(Path("/work/source/database-packages").glob("*." + target["format"]))
    if deb:
        Path("/usr/sbin/policy-rc.d").write_text("#!/bin/sh\nexit 101\n")
        Path("/usr/sbin/policy-rc.d").chmod(0o755)
        apt("update")
        apt("install", "python3", "ca-certificates", "util-linux", "passwd", "file", "binutils")
        if build:
            local = split_database(job, local)
        if build and target["pgdg"]:
            apt("install", "postgresql-common", "curl", "gnupg")
            run(["/usr/share/postgresql-common/pgdg/apt.postgresql.org.sh", "-y"])
        if build:
            apt(
                "install",
                "build-essential",
                "dpkg-dev",
                "pkg-config",
                "libipc-run-perl",
                "diffutils",
                *target["sdk_packages"],
                *target["runtime_packages"],
                *job["build_dependencies"],
                *job["runtime_dependencies"],
                *map(str, local),
            )
    else:
        run(["dnf", "-y", "install", "python3", "util-linux", "shadow-utils", "file", "binutils"])
        if build and target["pgdg"]:
            arch = {"amd64": "x86_64", "arm64": "aarch64"}[target["architecture"]]
            run(["dnf", "-y", "install", "dnf-plugins-core"])
            run(["dnf", "config-manager", "--set-enabled", "crb"])
            run(["dnf", "-y", "install", "epel-release"])
            repo = (
                "https://download.postgresql.org/pub/repos/yum/reporpms/EL-"
                + target["os"]["version"]
                + "-"
                + arch
                + "/pgdg-redhat-repo-latest.noarch.rpm"
            )
            run(["dnf", "-y", "install", repo])
            if target["os"]["version"] == "9":
                run(["dnf", "-y", "module", "disable", "postgresql"])
        if build:
            run(
                [
                    "dnf",
                    "-y",
                    "install",
                    "gcc",
                    "make",
                    "rpm-build",
                    "redhat-rpm-config",
                    "dnf-plugins-core",
                    "createrepo_c",
                    "perl-IPC-Run",
                    "diffutils",
                    *target["sdk_packages"],
                    *target["runtime_packages"],
                    *job["build_dependencies"],
                    *job["runtime_dependencies"],
                    *map(str, local),
                ]
            )
    uid = str(job["uid"])
    if run(["getent", "passwd", uid], check=False).returncode:
        run(["useradd", "--uid", uid, "--create-home", "builder"])
    user = run(["getent", "passwd", uid]).stdout.split(":", 1)[0]
    if uid == "0":
        raise RuntimeError("extension compilation requires a non-root build UID")
    write("/opt/pg-maker-extension-user.json", {"user": user, "uid": int(uid)})
    if build:
        repository = Path("/opt/pg-maker/extension-runtime-repository")
        repository.mkdir(parents=True, exist_ok=True)
        if deb:
            (repository / "partial").mkdir(exist_ok=True)
            Path("/tmp/pg-maker-empty-status").touch()
            apt(
                "--download-only",
                "-o",
                "Dir::State::status=/tmp/pg-maker-empty-status",
                "-o",
                "Dir::Cache::archives=" + str(repository),
                "install",
                *target["runtime_packages"],
                *job["runtime_dependencies"],
                *map(str, local),
            )
            for path in local:
                shutil.copy2(path, repository / path.name)
            result = run(["dpkg-scanpackages", "--multiversion", ".", "/dev/null"], cwd=repository)
            (repository / "Packages").write_text(result.stdout)
        else:
            run(
                [
                    "dnf",
                    "download",
                    "--resolve",
                    "--alldeps",
                    "--destdir",
                    str(repository),
                    *target["runtime_packages"],
                    *job["runtime_dependencies"],
                ]
            )
            for path in local:
                shutil.copy2(path, repository / path.name)
            run(["createrepo_c", str(repository)])
        records = [
            {"path": p.relative_to(repository).as_posix(), "sha256": digest(p)}
            for p in sorted(repository.rglob("*"))
            if p.is_file() and p.name != "lock"
        ]
        write("/opt/pg-maker/extension-runtime-repository.json", records)
    if deb:
        apt("clean")
    else:
        run(["dnf", "clean", "all"])


def inspect_target(job):
    target = job["config"]["target"]
    os_release = dict(
        line.split("=", 1)
        for line in Path("/etc/os-release").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    observed_os = {
        "id": os_release["ID"].strip('"'),
        "version": os_release["VERSION_ID"].strip('"').split(".")[0],
    }
    if observed_os != target["os"]:
        raise RuntimeError("container OS differs from extension target")
    pg_config = target["pg_config"]
    keys = [
        "version",
        "bindir",
        "libdir",
        "pkglibdir",
        "sharedir",
        "includedir-server",
        "includedir",
        "pgxs",
        "configure",
        "cc",
        "cflags",
        "ldflags",
    ]
    layout = {key: run([pg_config, "--" + key]).stdout.strip() for key in keys}
    match = re.search(r"PostgreSQL (\d+)(?:\.(\d+))?", layout["version"])
    if not match or int(match[1]) != target["major"]:
        raise RuntimeError("SDK PostgreSQL major differs from target")
    version = match[1] + "." + (match[2] or "0")
    if target.get("version") and target["version"] != version:
        raise RuntimeError("SDK PostgreSQL version differs from profile")
    headers = Path(layout["includedir-server"])
    if not (headers / "postgres.h").is_file() or not Path(layout["pgxs"]).is_file():
        raise RuntimeError("target lacks server headers or PGXS")
    server = Path(layout["bindir"]) / "postgres"
    if not server.is_file():
        raise RuntimeError("target server is absent")
    if target["format"] == "deb":
        result = run(
            [
                "dpkg-query",
                "-W",
                "-f=${Package}\t${Version}\t${Architecture}\n",
                target["server_package"],
            ]
        )
        name, package_version, arch = result.stdout.strip().split("\t")
    else:
        result = run(["rpm", "-q", "--qf", "%{NAME}\t%{EVR}\t%{ARCH}\n", target["server_package"]])
        name, package_version, arch = result.stdout.strip().split("\t")
    expected_arch = (
        target["architecture"]
        if target["format"] == "deb"
        else {"amd64": "x86_64", "arm64": "aarch64"}[target["architecture"]]
    )
    if arch != expected_arch:
        raise RuntimeError("installed server package architecture differs from target")
    files = [
        {"path": str(p), "sha256": digest(p)} for p in sorted(headers.rglob("*")) if p.is_file()
    ]
    files += [
        {"path": str(server), "sha256": digest(server)},
        {"path": layout["pgxs"], "sha256": digest(layout["pgxs"])},
    ]
    result = {
        "os": observed_os,
        "layout": layout,
        "postgresql_version": version,
        "server_package": {"name": name, "version": package_version, "architecture": arch},
        "sdk_files": files,
        "sql_extensions": {
            p.stem: p.read_text() for p in Path(layout["sharedir"], "extension").glob("*.control")
        },
    }
    write("/work/output/target.json", result)


def build_one(job):
    extension = job["extension"]
    spec = extension["spec"]
    name = spec["sql_extension"]
    source = Path("/work/build/components") / name
    stage = Path("/work/build/staging") / name
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    user = json.loads(Path("/opt/pg-maker-extension-user.json").read_text())["user"]
    run(["chown", "-R", user, str(source), str(stage)])
    make = [
        "make",
        "--output-sync=target",
        "-j",
        str(job["config"]["execution"]["jobs"]),
        "USE_PGXS=1",
        "PG_CONFIG=" + job["config"]["target"]["pg_config"],
    ]
    run([*make, *spec["make_targets"]], cwd=source, user=user)
    run([*make, "DESTDIR=" + str(stage), *spec["install_targets"]], cwd=source, user=user)
    for file in stage.rglob("*"):
        if file.is_symlink():
            resolved = file.resolve()
            if not resolved.is_relative_to(stage):
                raise RuntimeError("staged symlink escapes extension tree")
        elif not file.is_file() and not file.is_dir():
            raise RuntimeError("special file in extension staging")
        if file.stat().st_mode & 0o6000:
            raise RuntimeError("setuid/setgid file in extension staging")
    if not any(p.is_file() for p in stage.rglob("*")):
        raise RuntimeError("extension did not install any staged files")
    layout = job["target"]["layout"]
    for sql_name, version in spec.get("sql_extension_versions", {}).items():
        control = stage / layout["sharedir"].lstrip("/") / "extension" / (sql_name + ".control")
        if not control.is_file() or not re.search(
            r"^default_version\s*=\s*'" + re.escape(version) + "'", control.read_text(), re.M
        ):
            raise RuntimeError("installed SQL extension version differs from recipe")
    deb = job["config"]["target"]["format"] == "deb"
    license_dir = (
        stage / ("usr/share/doc" if deb else "usr/share/licenses") / extension["package"]["name"]
    )
    license_dir.mkdir(parents=True, exist_ok=True)
    licenses = [
        p
        for p in source.iterdir()
        if p.is_file() and p.name.upper().startswith(("LICENSE", "COPYING", "COPYRIGHT"))
    ]
    if not licenses:
        raise RuntimeError("extension source needs a license file")
    if deb:
        # Debian slim deliberately excludes documentation except this standard path.
        (license_dir / "copyright").write_text(
            "\n\n".join(file.name + "\n" + file.read_text() for file in sorted(licenses))
        )
    else:
        for file in licenses:
            shutil.copy2(file, license_dir / file.name)


def file_inventory(root):
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "symlink",
                    "target": os.readlink(path),
                    "mode": path.lstat().st_mode & 0o7777,
                }
            )
        elif path.is_file():
            result.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "type": "file",
                    "sha256": digest(path),
                    "size": path.stat().st_size,
                    "mode": path.stat().st_mode & 0o7777,
                }
            )
        elif not path.is_dir():
            raise RuntimeError("unsupported payload entry")
    return result


def verify_rpm_inventory(files, query):
    """Compare native SHA256/mode/ownership records independent of path ordering."""
    actual = []
    for line in query.splitlines():
        filename, size, mode, sha, link, owner, group = line.split("\t")
        if owner != "root" or group != "root":
            raise RuntimeError("RPM payload must be root-owned")
        entry = {"path": filename.lstrip("/"), "mode": int(mode) & 0o7777}
        if int(mode) & 0o170000 == 0o120000:
            entry.update(type="symlink", target=link)
        else:
            entry.update(type="file", size=int(size), sha256="sha256:" + sha)
        actual.append(entry)
    if sorted(actual, key=lambda e: e["path"]) != sorted(files, key=lambda e: e["path"]):
        raise RuntimeError("RPM payload differs from packaging root")


def validate_runpaths(dynamic, layout):
    """Allow only the installed SDK's absolute library directories, never work trees."""
    approved = set()
    for value in re.findall(r"\((?:RPATH|RUNPATH)\).*\[([^\]]*)\]", dynamic):
        for path in value.split(":"):
            if (
                path not in {layout["libdir"], layout["pkglibdir"]}
                or not path.startswith("/")
                or any(part in {"..", "tmp", "work"} for part in Path(path).parts)
            ):
                raise RuntimeError("extension contains an unapproved runtime search path: " + path)
            approved.add(path)
    return approved


def package_one(job):
    target = job["config"]["target"]
    extension = job["extension"]
    name = extension["spec"]["sql_extension"]
    identity = extension["package"]
    stage = Path("/work/build/staging") / name
    destination = Path("/work/output/packages")
    destination.mkdir(parents=True, exist_ok=True)
    files = file_inventory(stage)
    # Conflicts are checked even for identical bytes: native package ownership matters.
    overlaps = [
        f["path"]
        for f in files
        if not f["path"].startswith("usr/share/doc/") and Path("/", f["path"]).exists()
    ]
    if overlaps:
        raise RuntimeError(
            "target already owns extension files; split bundled modules first: "
            + repr(overlaps[:10])
        )
    dependencies = list(identity["dependencies"])
    sql_dependencies = {}
    layout = job["target"]["layout"]
    for control in stage.rglob("*.control"):
        required = re.search(r"^requires\s*=\s*'([^']+)'", control.read_text(), re.M)
        for sql_name in required[1].split(",") if required else []:
            sql_name = sql_name.strip()
            if sql_name in job["extension_packages"]:
                provider = job["extension_packages"][sql_name]
                if provider["name"] == identity["name"]:
                    continue
                dependencies.append(
                    provider["name"]
                    + (
                        " (= " + provider["version"] + ")"
                        if target["format"] == "deb"
                        else " = " + provider["version"]
                    )
                )
            else:
                installed = Path(layout["sharedir"], "extension", sql_name + ".control")
                if not installed.is_file():
                    raise RuntimeError("unresolved SQL extension dependency: " + sql_name)
                if target["format"] == "deb":
                    owner = run(["dpkg-query", "-S", str(installed)]).stdout.split(": ", 1)[0]
                    version = run(["dpkg-query", "-W", "-f=${Version}", owner]).stdout.strip()
                    dependencies.append(owner + " (>= " + version + ")")
                else:
                    owner = run(["rpm", "-qf", "--qf", "%{NAME}", str(installed)]).stdout.strip()
                    version = run(["rpm", "-q", "--qf", "%{EVR}", owner]).stdout.strip()
                    dependencies.append(owner + " >= " + version)
            sql_dependencies[sql_name] = "resolved"
    elfs = [
        stage / f["path"]
        for f in files
        if f["type"] == "file" and (stage / f["path"]).read_bytes()[:4] == b"\x7fELF"
    ]
    approved_runpaths = set()
    for elf in elfs:
        dynamic = run(["readelf", "-d", str(elf)]).stdout
        approved_runpaths.update(validate_runpaths(dynamic, layout))
    if target["format"] == "deb":
        # dpkg-shlibdeps recognizes a staged package root by this directory.
        control = stage / "DEBIAN"
        control.mkdir()
        scratch = Path("/work/build/packaging") / name
        (scratch / "debian").mkdir(parents=True, exist_ok=True)
        (scratch / "debian/control").write_text(
            "Source: "
            + identity["name"]
            + "\n\nPackage: "
            + identity["name"]
            + "\nArchitecture: any\nDescription: PostgreSQL extension\n"
        )
        shlibs = []
        for library in (
            sorted(Path(layout["libdir"]).glob("*.so.*"))
            if layout["libdir"].startswith("/opt/")
            else []
        ):
            if library.is_symlink():
                continue
            output = run(["readelf", "-d", str(library)]).stdout
            soname = re.search(r"\(SONAME\).*\[(.+)\.so\.(.+)\]", output)
            if soname:
                owner_result = run(["dpkg-query", "-S", str(library)], check=False)
                if owner_result.returncode:
                    continue
                owner = owner_result.stdout.split(": ", 1)[0]
                version = run(["dpkg-query", "-W", "-f=${Version}", owner]).stdout.strip()
                shlibs.append(f"{soname[1]} {soname[2]} {owner} (>= {version})")
        (scratch / "debian/shlibs.local").write_text("".join(line + "\n" for line in shlibs))
        if elfs:
            output = run(
                [
                    "dpkg-shlibdeps",
                    "-O",
                    "-l" + layout["libdir"],
                    "-l" + layout["pkglibdir"],
                    *map(str, elfs),
                ],
                cwd=scratch,
            ).stdout
            found = re.search(r"^shlibs:Depends=(.+)$", output, re.M)
            if found:
                dependencies.extend(x.strip() for x in found[1].split(","))
        dependencies.extend(extension["spec"]["runtime_dependencies"])
        text = (
            f"Package: {identity['name']}\nVersion: {identity['version']}\n"
            f"Architecture: {target['architecture']}\nSection: database\nPriority: optional\n"
            f"Maintainer: {job['config']['maintainer']}\n"
            f"Depends: {', '.join(sorted(set(dependencies)))}\n"
            f"Description: {name} for {target['server_package']}\n"
        )
        migrated = [
            r for r in job.get("migration", []) if any(f["extension"] == name for f in r["removed"])
        ]
        if migrated:
            moved = ", ".join(r["name"] + " (<< " + r["version"] + ")" for r in migrated)
            text += "Breaks: " + moved + "\nReplaces: " + moved + "\n"
        (control / "control").write_text(text)
        output = destination / identity["filename"]
        run(["dpkg-deb", "--build", "--root-owner-group", "-Zxz", str(stage), str(output)])
        shutil.rmtree(control)
        metadata = run(["dpkg-deb", "--field", str(output)]).stdout
    else:
        rpmroot = Path("/work/build/rpmbuild") / name
        rpmroot.mkdir(parents=True, exist_ok=True)
        rpm_spec = rpmroot / "extension.spec"
        # RPM owns payload files, not common PostgreSQL directories.
        file_lines = []
        for entry in files:
            escaped = ("/" + entry["path"]).replace("%", "%%").replace('"', '\\"')
            if "\n" in escaped:
                raise RuntimeError("newline in package filename")
            file_lines.append(
                ("%license " if entry["path"].startswith("usr/share/licenses/") else "")
                + '"'
                + escaped
                + '"'
            )
        dependencies.extend(extension["spec"]["runtime_dependencies"])
        rpm_spec.write_text(
            f"Name: {identity['name']}\nVersion: {identity['upstream_version']}\n"
            f"Release: {identity['release']}\nSummary: {name} PostgreSQL extension\n"
            f"License: {extension['packaging']['license']}\nAutoReqProv: yes\n"
            + "".join("Requires: " + dep + "\n" for dep in sorted(set(dependencies)))
            + "\n%description\nStandalone PostgreSQL extension.\n"
            + "\n%prep\n%build\n%install\nmkdir -p %{buildroot}\n"
            + "cp -a "
            + str(stage)
            + "/. %{buildroot}/\n"
            + "\n%files\n%defattr(-,root,root,-)\n"
            + "\n".join(file_lines)
            + "\n\n%changelog\n* "
            + time.strftime("%a %b %d %Y", time.gmtime())
            + " pg-maker <builder@example.invalid> - "
            + identity["version"]
            + "\n- Package the extension against the pinned PostgreSQL SDK.\n"
        )
        run(
            [
                "rpmbuild",
                "-bb",
                "--noclean",
                "--define",
                "_topdir " + str(rpmroot),
                "--define",
                "_build_id_links none",
                "--define",
                "debug_package %{nil}",
                str(rpm_spec),
            ],
            # RPM's generic FHS check rejects PGDG's /usr/pgsql-MAJOR/lib.
            # Only the exact SDK directories validated above receive this exception.
            env={"QA_RPATHS": "0x0003" if approved_runpaths else "0x0000"},
        )
        rpms = list((rpmroot / "RPMS").rglob("*.rpm"))
        if len(rpms) != 1:
            raise RuntimeError("expected exactly one extension RPM")
        output = destination / identity["filename"]
        shutil.copy2(rpms[0], output)
        metadata = run(["rpm", "-qp", "--requires", str(output)]).stdout
        # rpmbuild can strip/debug-edit ELF files; inspect the actual packaged root.
        roots = list((rpmroot / "BUILDROOT").glob("*"))
        if len(roots) != 1:
            raise RuntimeError("missing RPM buildroot")
        files = file_inventory(roots[0])
        actual_identity = (
            run(["rpm", "-qp", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}", str(output)])
            .stdout.strip()
            .split("\t")
        )
        if actual_identity != [identity["name"], identity["version"], identity["architecture"]]:
            raise RuntimeError("RPM identity differs from extension recipe")
        algorithm = run(["rpm", "-qp", "--qf", "%{FILEDIGESTALGO}", str(output)]).stdout.strip()
        if algorithm != "8":
            raise RuntimeError("RPM payload must use SHA256 file digests")
        query = (
            "[%{FILENAMES}\t%{FILESIZES}\t%{FILEMODES}\t%{FILEDIGESTS}\t"
            "%{FILELINKTOS}\t%{FILEUSERNAME}\t%{FILEGROUPNAME}\n]"
        )
        verify_rpm_inventory(files, run(["rpm", "-qp", "--qf", query, str(output)]).stdout)
    write(
        destination / (name + ".json"),
        {
            "name": identity["name"],
            "version": identity["version"],
            "path": output.name,
            "sha256": digest(output),
            "files": files,
            "sql_dependencies": sql_dependencies,
            "validated_runpaths": sorted(approved_runpaths),
            "metadata": metadata,
        },
    )


def account():
    return json.loads(Path("/opt/pg-maker-extension-user.json").read_text())["user"]


def remove_test_tablespaces(data, workspace):
    """Remove only PostgreSQL-owned version directories of this test cluster."""
    targets = []
    for link in (data / "pg_tblspc").glob("*"):
        if not link.is_symlink():
            raise RuntimeError("unexpected test tablespace entry")
        target = link.resolve()
        if not target.is_relative_to(workspace.resolve()) or target == workspace.resolve():
            raise RuntimeError("test tablespace is outside the owned workspace")
        for child in target.glob("PG_*"):
            if not re.fullmatch(r"PG_[0-9]+(?:\.[0-9]+)?_[0-9]+", child.name):
                raise RuntimeError("unexpected test tablespace version directory")
            if child.is_symlink() or not child.is_dir():
                raise RuntimeError("test tablespace version directory is not an ordinary directory")
            targets.append(child)
    # Validate every target before deleting any test data. Preserve unrelated files.
    for child in set(targets):
        shutil.rmtree(child)


def cluster(job, *, start):
    bindir = job["target"]["layout"]["bindir"]
    data = Path("/work/build/cluster/data")
    if not start:
        return run(
            [bindir + "/pg_ctl", "-D", str(data), "-m", "fast", "-w", "stop"],
            user=account(),
            check=False,
        )
    root = data.parent
    if root.exists():
        remove_test_tablespaces(data, Path("/work/build"))
        shutil.rmtree(root)
    root.mkdir(parents=True)
    (root / "socket").mkdir()
    run(["chown", "-R", account(), str(root)])
    run(
        [bindir + "/initdb", "-D", str(data), "--no-locale", "-E", "UTF8", "-A", "trust"],
        user=account(),
    )
    preload = list(
        dict.fromkeys(
            lib for e in job["config"]["extensions"] for lib in e["spec"].get("preload", [])
        )
    )
    settings = {
        k: v
        for e in job["config"]["extensions"]
        for k, v in e["spec"].get("test_settings", {}).items()
    }
    with (data / "postgresql.conf").open("a") as stream:
        stream.write(
            "\nlisten_addresses = ''\nport = 55432\nunix_socket_directories = '"
            + str(root / "socket")
            + "'\nshared_preload_libraries = '"
            + ",".join(preload)
            + "'\n"
        )
        for key, value in settings.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", key):
                raise RuntimeError("invalid test GUC")
            stream.write(key + " = '" + str(value).replace("'", "''").replace("\\", "\\\\") + "'\n")
    run(
        [bindir + "/pg_ctl", "-D", str(data), "-l", str(root / "server.log"), "-w", "start"],
        user=account(),
    )


def database_env(job):
    return {
        "PGHOST": "/work/build/cluster/socket",
        "PGPORT": "55432",
        "PGUSER": account(),
        "PGDATABASE": "postgres",
        "PATH": job["target"]["layout"]["bindir"] + ":" + os.environ["PATH"],
        "LC_ALL": "C",
        "TZ": "UTC",
        "PGTZ": "UTC",
    }


def sql(job, text):
    return run(
        [
            job["target"]["layout"]["bindir"] + "/psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            text,
        ],
        user=account(),
        env=database_env(job),
    )


def source_tests(job):
    for extension in job["config"]["extensions"]:
        stage = Path("/work/build/staging") / extension["spec"]["sql_extension"]
        for entry in file_inventory(stage):
            path = stage / entry["path"]
            destination = Path("/", entry["path"])
            destination.parent.mkdir(parents=True, exist_ok=True)
            if entry["type"] == "symlink":
                destination.unlink(missing_ok=True)
                destination.symlink_to(entry["target"])
            else:
                shutil.copy2(path, destination)
    outcomes = []
    try:
        cluster(job, start=True)
        for extension in job["config"]["extensions"]:
            spec = extension["spec"]
            name = spec["sql_extension"]
            source = Path("/work/build/components") / name
            values = {
                "{prefix}": str(Path(job["target"]["layout"]["bindir"]).parent),
                "{component}": str(source),
                "{jobs}": str(job["config"]["execution"]["jobs"]),
            }

            def render(arg, values=values):
                for key, value in values.items():
                    arg = arg.replace(key, value)
                return arg

            for setup in spec.get("test_setup", []):
                run([render(x) for x in setup], cwd=source, user=account(), env=database_env(job))
            commands = spec.get("test_commands") or [
                {
                    "id": "installcheck",
                    "argv": [
                        "make",
                        "--output-sync=target",
                        "USE_PGXS=1",
                        "PG_CONFIG=" + job["config"]["target"]["pg_config"],
                        *spec["test_targets"],
                    ],
                    "success_pattern": r"All (?P<count>\d+) tests passed\.",
                }
            ]
            if not spec.get("test_commands") and not spec["test_targets"]:
                raise RuntimeError("extension recipe has no source test suite")
            for command in commands:
                result = run(
                    [render(x) for x in command["argv"]],
                    cwd=source,
                    user=account(),
                    env=database_env(job),
                )
                matches = list(
                    re.finditer(command["success_pattern"], result.stdout + result.stderr, re.M)
                )
                count = sum(int(m.groupdict().get("count", "1")) for m in matches)
                if not matches or count < 1:
                    raise RuntimeError("source tests did not report a nonempty successful suite")
                outcomes.append(
                    {"extension": name, "suite": command["id"], "passed": True, "count": count}
                )
    finally:
        cluster(job, start=False)
        logs = Path("/work/output/source-test-logs")
        logs.mkdir(parents=True, exist_ok=True)
        for path in Path("/work/build").rglob("*"):
            if path.is_file() and (
                path.name in {"regression.diffs", "regression.out", "server.log"}
                or (path.suffix == ".log" and "log" in path.parts)
            ):
                destination = logs / path.relative_to("/work/build")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
                shutil.chown(destination, user=account())
    write("/work/output/source-tests.json", {"passed": True, "suites": outcomes})


def install_packages(job, packages, *, action="install"):
    if job["config"]["target"]["format"] == "deb":
        Path("/tmp/extension-test.sources.list").write_text(
            "deb [trusted=yes] file:/work/source/repository ./\n"
        )
        args = [
            "-o",
            "Dir::Etc::sourcelist=/tmp/extension-test.sources.list",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "APT::Get::List-Cleanup=0",
        ]
        apt(*args, "update")
        apt(*args, action, *packages)
    else:
        run(
            [
                "dnf",
                "-y",
                "--disablerepo=*",
                "--repofrompath=pg-maker,file:///work/source/repository",
                "--setopt=pg-maker.gpgcheck=0",
                action,
                *packages,
            ]
        )


def package_tests(job):
    target = job["config"]["target"]
    deb = target["format"] == "deb"
    records = job["packages"]
    package_paths = ["/work/source/packages/" + r["path"] for r in records]
    negative = []
    for index, record in enumerate(records):
        package = package_paths[index]
        if deb:
            result = run(["dpkg", "--install", package], check=False)
            run(["dpkg", "--purge", record["name"]], check=False)
        else:
            result = run(["rpm", "--install", "--test", package], check=False)
        if not result.returncode or target["server_package"] not in result.stderr:
            raise RuntimeError("package did not reject its missing target server dependency")
        negative.append({"package": record["name"], "missing_server_rejected": True})
    install_packages(job, package_paths)
    for record in records:
        for entry in record["files"]:
            path = Path("/", entry["path"])
            if entry["type"] == "file":
                if not path.is_file() or digest(path) != entry["sha256"]:
                    raise RuntimeError(
                        "installed package file differs from sealed staging: " + str(path)
                    )
            elif not path.is_symlink() or os.readlink(path) != entry["target"]:
                raise RuntimeError("installed package symlink differs")
            if path.lstat().st_uid != 0 or path.lstat().st_gid != 0:
                raise RuntimeError("installed payload is not root-owned")
            if path.lstat().st_mode & 0o7777 != entry["mode"]:
                raise RuntimeError("installed payload permissions differ")
    checks = []
    try:
        cluster(job, start=True)
        sql(
            job,
            "CREATE TABLE public.extension_package_sentinel(id integer PRIMARY KEY); "
            "INSERT INTO public.extension_package_sentinel VALUES (42)",
        )
        extensions = job["config"]["extensions"]
        for extension in extensions:
            name = extension["spec"]["sql_extension"]
            # CASCADE resolves SQL dependencies, whose files were already checked by packaging.
            sql(job, 'CREATE EXTENSION "' + name + '" CASCADE')
            result = sql(job, "SELECT extversion FROM pg_extension WHERE extname = '" + name + "'")
            expected = extension["spec"]["sql_extension_versions"][name]
            if result.stdout.strip() != expected:
                raise RuntimeError("installed SQL extension has an unexpected version")
            if name == "pg_stat_kcache":
                sql(job, "SELECT sum(i::bigint*i) FROM generate_series(1,100000) AS g(i)")
                if int(sql(job, "SELECT count(*) FROM pg_stat_kcache").stdout.strip()) < 1:
                    raise RuntimeError("pg_stat_kcache did not record activity")
            elif name == "pg_wait_sampling":
                sql(job, "SELECT pg_sleep(0.3)")
                if (
                    int(
                        sql(
                            job,
                            "SELECT count(*) FROM pg_wait_sampling_profile WHERE event = 'PgSleep'",
                        ).stdout.strip()
                    )
                    < 1
                ):
                    raise RuntimeError("pg_wait_sampling did not sample the expected wait event")
            elif name == "pg_repack":
                sql(
                    job,
                    "CREATE TABLE public.extension_repack_test"
                    "(id integer PRIMARY KEY, value text); "
                    "INSERT INTO public.extension_repack_test SELECT i, md5(i::text) "
                    "FROM generate_series(1,1000) g(i)",
                )
                before = sql(
                    job,
                    "SELECT md5(string_agg(id::text || value, ',' ORDER BY id)) "
                    "FROM public.extension_repack_test",
                ).stdout
                run(
                    [
                        job["target"]["layout"]["bindir"] + "/pg_repack",
                        "--table=public.extension_repack_test",
                        "--dbname=postgres",
                        "--no-order",
                    ],
                    user=account(),
                    env=database_env(job),
                )
                after = sql(
                    job,
                    "SELECT md5(string_agg(id::text || value, ',' ORDER BY id)) "
                    "FROM public.extension_repack_test",
                ).stdout
                if before != after:
                    raise RuntimeError("pg_repack changed test data")
            checks.append(
                {
                    "extension": name,
                    "create_extension": True,
                    "version": expected,
                    "functional": name in {"pg_stat_kcache", "pg_wait_sampling", "pg_repack"},
                }
            )
        cluster(job, start=False)
        data = "/work/build/cluster/data"
        run(
            [
                job["target"]["layout"]["bindir"] + "/pg_ctl",
                "-D",
                data,
                "-l",
                "/work/build/cluster/server.log",
                "-w",
                "start",
            ],
            user=account(),
        )
        if sql(job, "SELECT id FROM public.extension_package_sentinel").stdout.strip() != "42":
            raise RuntimeError("restart lost sentinel data")
        cluster(job, start=False)
        install_packages(job, [r["name"] for r in records], action="remove")
        install_packages(job, package_paths)
        run(
            [
                job["target"]["layout"]["bindir"] + "/pg_ctl",
                "-D",
                data,
                "-l",
                "/work/build/cluster/server.log",
                "-w",
                "start",
            ],
            user=account(),
        )
        if sql(job, "SELECT id FROM public.extension_package_sentinel").stdout.strip() != "42":
            raise RuntimeError("reinstall lost sentinel data")
    finally:
        cluster(job, start=False)
        if Path("/work/build/cluster/server.log").is_file():
            shutil.copy2("/work/build/cluster/server.log", "/work/output/server.log")
            shutil.chown("/work/output/server.log", user=account())
    write(
        "/work/output/package-tests.json",
        {
            "passed": True,
            "negative_dependencies": negative,
            "extensions": checks,
            "installed_inventory": True,
            "restart": True,
            "remove_reinstall_preserves_data": True,
            "previous_version_upgrade": "not-configured",
        },
    )


def main():
    action = sys.argv[1]
    job = json.loads(Path(sys.argv[2]).read_text())
    if action.startswith("provision-"):
        provision(job, build=action == "provision-build")
    elif action == "inspect":
        inspect_target(job)
    elif action == "build":
        build_one(job)
    elif action == "package":
        package_one(job)
    elif action == "test-source":
        source_tests(job)
    elif action == "test-packages":
        package_tests(job)
    else:
        raise RuntimeError("unknown container helper action")


if __name__ == "__main__":
    main()
