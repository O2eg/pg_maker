"""Offline Debian runtime image from verified package bytes and a pinned stand profile."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import replace
from pathlib import Path

from pg_maker.artifacts.manifest import verify_manifest
from pg_maker.errors import CancelledError, PreconditionError, ValidationError
from pg_maker.models import CleanupPolicy, CommandSpec, EnvironmentSpec
from pg_maker.packaging.deb import inspect_deb
from pg_maker.planning.runtime_repository import verify_repository
from pg_maker.runners.docker import DockerRunner
from pg_maker.storage import content_hash, file_hash, read_json


def plan_runtime(directory: Path, profile: dict) -> dict:
    directory = directory.absolute()
    manifest = read_json(directory / "artifact-manifest.json")
    verify_manifest(directory, manifest)
    if set(profile) != {
        "base_image",
        "files",
        "min_free_disk_bytes",
        "memory_bytes",
        "entrypoint",
        "extensions",
        "libraries",
    }:
        raise ValidationError("invalid runtime profile fields")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", profile["base_image"]):
        raise ValidationError("runtime base must be an immutable local Docker image ID")
    if (
        not profile["files"]
        or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) for name in profile["files"])
        or profile["entrypoint"] not in profile["files"]
    ):
        raise ValidationError("runtime profile requires safe filenames and its entrypoint")
    for key in ("extensions", "libraries"):
        values = profile[key]
        if (
            not isinstance(values, list)
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)
                for v in values
            )
            or len(values) != len(set(values))
        ):
            raise ValidationError("invalid runtime extension/library names")
    for reference in profile["files"].values():
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise ValidationError("runtime file needs an absolute path and sha256")
        path = Path(reference["path"])
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ValidationError("runtime file must be an existing absolute regular file")
        if file_hash(path) != reference["sha256"]:
            raise PreconditionError("runtime profile file hash changed")
    for key in ("min_free_disk_bytes", "memory_bytes"):
        if type(profile[key]) is not int or profile[key] < 1:
            raise ValidationError("runtime limits must be positive integer bytes")
    packages = [
        a for a in manifest["artifacts"] if a["package_role"] in {"server", "client", "libpq"}
    ]
    if len(packages) != 3 or {a["package_role"] for a in packages} != {"server", "client", "libpq"}:
        raise ValidationError("runtime needs exactly server, client and libpq packages")
    identities = {
        (
            a["producer"]["plan_hash"],
            a["feature"],
            a["target_id"],
            a["source_hash"],
            a["install_tree_hash"],
        )
        for a in packages
    }
    if len(identities) != 1 or any(
        a["os"] != {"id": "debian", "version": "13"} or a["architecture"] != "x86_64"
        for a in packages
    ):
        raise ValidationError("runtime v1 requires one Debian 13 x86_64 package variant")
    entries = {e["path"] for a in packages for e in inspect_deb(directory / a["path"])["entries"]}
    binaries = [p for p in entries if p.endswith("/bin/postgres")]
    if len(binaries) != 1:
        raise ValidationError("cannot determine a unique packaged PostgreSQL prefix")
    prefix = "/" + binaries[0].removesuffix("/bin/postgres")
    if not re.fullmatch(r"/opt/[A-Za-z0-9_./+-]+", prefix) or ".." in prefix.split("/"):
        raise ValidationError("invalid PostgreSQL runtime prefix")
    missing = [
        name
        for name in {*profile["extensions"], *profile["libraries"]}
        if not any(
            p.startswith(prefix.lstrip("/") + "/") and p.endswith("/" + name + ".so")
            for p in entries
        )
    ]
    if missing:
        raise PreconditionError(
            "stand feature lacks extensions built against its pg_config",
            details={"missing": missing},
        )
    for name in profile["extensions"]:
        shared = prefix.lstrip("/") + "/share/postgresql/extension/" + name
        if shared + ".control" not in entries or not any(
            re.fullmatch(re.escape(shared) + r"--[^/]+\.sql", p)
            and "--" not in p[len(shared) + 2 : -4]
            for p in entries
        ):
            raise PreconditionError(
                "runtime extension lacks control or install script", details={"extension": name}
            )
    repository_file = directory / "runtime-repository.json"
    if not repository_file.is_file():
        raise PreconditionError("missing pinned runtime repository; rebuild packages")
    dependencies = [
        {"path": "runtime-repository/" + item["path"], "sha256": item["sha256"]}
        for item in verify_repository(directory / "runtime-repository", read_json(repository_file))
    ]
    result = {
        "api_version": "pg_maker/runtime-plan/v1",
        "artifact_directory": str(directory),
        "manifest_hash": manifest["manifest_hash"],
        "packages": packages,
        "dependencies": dependencies,
        "profile": profile,
        "prefix": prefix,
        "postgresql_version": packages[0]["postgresql_version"],
        "qualification": packages[0]["qualification"],
        "build_plan_hash": packages[0]["producer"]["plan_hash"],
        "implementation_hash": file_hash(Path(__file__)),
    }
    result["plan_hash"] = content_hash(result)
    return result


def verify_runtime_plan(plan: dict, expected: str) -> None:
    if plan.get("api_version") != "pg_maker/runtime-plan/v1" or plan.get("plan_hash") != expected:
        raise PreconditionError("runtime plan hash differs from reviewed plan")
    if plan_runtime(Path(plan["artifact_directory"]), plan["profile"]) != plan:
        raise PreconditionError("runtime inputs changed since planning")


def build_runtime(plan: dict, context) -> dict:
    verify_runtime_plan(plan, plan["plan_hash"])
    runner = DockerRunner(
        context.directory,
        operation_id=context.state["operation_id"],
        owner_token=context.state["owner_token"],
        cancelled=context.token.requested,
        on_process=context.on_process,
        attempt=context.state["attempt"],
    )
    runner.doctor()
    profile, prefix = plan["profile"], plan["prefix"]

    def check_space():
        if shutil.disk_usage(context.directory).free < profile["min_free_disk_bytes"]:
            raise PreconditionError("insufficient free disk space for runtime image")

    check_space()
    handle = runner.intended_handle(EnvironmentSpec(profile["base_image"], "0:0", "stand-runtime"))

    def record(name):
        context.event(
            name,
            resources=[
                {
                    "kind": "docker",
                    "name": handle.name,
                    "labels": handle.labels,
                    "container_id": handle.container_id,
                    "environment": {
                        "image": handle.environment.image,
                        "user": "0:0",
                        "target_id": "stand-runtime",
                    },
                }
            ],
        )

    record("runtime_creating")
    try:
        args = [
            "create",
            "--pull=never",
            "--name",
            handle.name,
            "--user",
            "0:0",
            "--network",
            "none",
            "--memory",
            str(profile["memory_bytes"]),
            "--entrypoint",
            "sleep",
        ]
        for key, value in handle.labels.items():
            args += ["--label", key + "=" + value]
        handle = replace(
            handle,
            container_id=runner._control(
                [*args, profile["base_image"], "infinity"]
            ).stdout_tail.strip(),
        )
        record("runtime_created")
        runner._control(["start", handle.container_id])
        counter = 0

        def execute(argv):
            nonlocal counter
            context.check()
            counter += 1
            result = runner.execute(
                handle,
                CommandSpec(
                    f"runtime-{counter}",
                    tuple(argv),
                    cwd="/",
                    timeout_seconds=context.timeout,
                    env={
                        "DEBIAN_FRONTEND": "noninteractive",
                        "PATH": prefix
                        + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    },
                ),
            )
            if result.cancelled:
                raise CancelledError("runtime image cancelled")
            if not result.succeeded:
                raise PreconditionError(
                    "runtime image command failed", details={"stderr": result.stderr_tail}
                )
            return result

        execute(
            [
                "sh",
                "-ec",
                '. /etc/os-release; test "$ID:$VERSION_ID" = debian:13; '
                'test "$(dpkg --print-architecture)" = amd64; '
                "command -v gosu; command -v pgbouncer; "
                "test -x /usr/bin/python3; test -x /usr/sbin/sshd; "
                "mkdir -p /tmp/pg-maker-packages /tmp/pg-maker-repository /run/sshd; "
                'printf "#!/bin/sh\\nexit 101\\n" > /usr/sbin/policy-rc.d; '
                "chmod 755 /usr/sbin/policy-rc.d",
            ]
        )
        directory = Path(plan["artifact_directory"])
        filenames = []
        for index, item in enumerate([*plan["dependencies"], *plan["packages"]]):
            check_space()
            source = directory / item["path"]
            if file_hash(source) != item["sha256"]:
                raise PreconditionError("runtime package changed")
            dependency = index < len(plan["dependencies"])
            dest = (
                "/tmp/pg-maker-repository/" + source.name
                if dependency
                else f"/tmp/pg-maker-packages/{index}.deb"
            )
            context.command(["docker", "cp", str(source), handle.container_id + ":" + dest])
            if not dependency:
                filenames.append(dest)
        # Reuse the installed-package acceptance strategy: apt sees only the
        # pinned local repository, and chooses dependencies rather than forcibly
        # upgrading every package in its closure. No remote sources or network.
        execute(
            [
                "sh",
                "-ec",
                "chmod 755 /tmp/pg-maker-packages /tmp/pg-maker-repository; "
                "chmod 644 /tmp/pg-maker-packages/*; "
                "find /tmp/pg-maker-repository -type f -exec chmod 644 {} +; "
                "mkdir -p /tmp/pg-maker-apt-lists/partial; "
                "if test -f /tmp/pg-maker-repository/Packages; then "
                "printf '%s\\n' 'deb [trusted=yes] file:/tmp/pg-maker-repository ./' "
                "> /tmp/pg-maker-sources.list; else : > /tmp/pg-maker-sources.list; fi",
            ]
        )
        apt = [
            "apt-get",
            "-y",
            "--no-install-recommends",
            "-o",
            "Dir::Etc::sourcelist=/tmp/pg-maker-sources.list",
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "Dir::State::lists=/tmp/pg-maker-apt-lists",
        ]
        for package in plan["packages"]:
            apt.extend(
                ["-o", "Dpkg::Options::=--path-include=/usr/share/doc/" + package["name"] + "/*"]
            )
        execute([*apt, "update"])
        execute([*apt, "install", *filenames])
        for name, reference in profile["files"].items():
            source = Path(reference["path"])
            if file_hash(source) != reference["sha256"]:
                raise PreconditionError("runtime profile file changed")
            context.command(
                ["docker", "cp", str(source), handle.container_id + ":/usr/local/bin/" + name]
            )
        quoted = shlex.quote(prefix)
        execute(
            [
                "sh",
                "-ec",
                f"""
test "$(pg_config --bindir)" = {quoted}/bin
for command in postgres initdb pg_ctl psql pg_config pg_basebackup pg_isready; do
    test -x {quoted}/bin/$command
    ldd {quoted}/bin/$command > /tmp/pg-maker-ldd
    ! grep -q 'not found' /tmp/pg-maker-ldd
done
for extension in {" ".join(profile["extensions"])}; do
    test -f "$(pg_config --sharedir)/extension/$extension.control"
done
for library in {" ".join(sorted({*profile["extensions"], *profile["libraries"]}))}; do
    test -f "$(pg_config --pkglibdir)/$library.so"
    ldd "$(pg_config --pkglibdir)/$library.so" > /tmp/pg-maker-ldd
    ! grep -q 'not found' /tmp/pg-maker-ldd
done
getent passwd postgres
chmod 755 {" ".join("/usr/local/bin/" + name for name in profile["files"])}
passwd -d root
printf '%s\\n' 'PasswordAuthentication no' > /etc/ssh/sshd_config.d/pg_stand.conf
printf '%s\\n' 'KbdInteractiveAuthentication no' >> /etc/ssh/sshd_config.d/pg_stand.conf
printf '%s\\n' 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config.d/pg_stand.conf
printf '%s\\n' 'PubkeyAuthentication yes' >> /etc/ssh/sshd_config.d/pg_stand.conf
rm -f /etc/ssh/ssh_host_*
rm -rf /tmp/pg-maker-packages /tmp/pg-maker-repository /tmp/pg-maker-apt-lists
rm -f /tmp/pg-maker-sources.list /tmp/pg-maker-ldd
apt-get clean
postgres --version
""",
            ]
        )
        runner.cancel(handle)
        check_space()
        path = prefix + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        image_id = context.command(
            [
                "docker",
                "commit",
                "--change",
                "USER root",
                "--change",
                "ENV PATH=" + path,
                "--change",
                "ENV PGDATA=/var/lib/postgresql/data",
                "--change",
                "ENV PGHOST=/var/run/postgresql LANG=C.UTF-8 LC_ALL=C.UTF-8",
                "--change",
                "ENTRYPOINT " + json.dumps(["/usr/local/bin/" + profile["entrypoint"]]),
                "--change",
                'CMD ["postgres"]',
                "--change",
                "LABEL io.pg-maker.runtime-plan=" + plan["plan_hash"],
                handle.container_id,
            ]
        ).stdout_tail.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise PreconditionError("invalid committed runtime image ID")
        result = {
            "api_version": "pg_maker/runtime-image/v1",
            "image_id": image_id,
            "runtime_plan_hash": plan["plan_hash"],
            "build_plan_hash": plan["build_plan_hash"],
            "artifact_manifest_hash": plan["manifest_hash"],
            "prefix": prefix,
            "postgresql_version": plan["postgresql_version"],
            "qualification": plan["qualification"],
            "database_started": False,
            "extensions": profile["extensions"],
            "libraries": profile["libraries"],
            "user": "postgres",
        }
        result["document_hash"] = content_hash(result)
        context.advance("runtime_image_committed")
        return result
    finally:
        runner.release(handle, CleanupPolicy.REMOVE)
        context.event("runtime_container_released", resources=[])
