"""Explicit, journaled apt image preparation on the local Docker daemon."""

from __future__ import annotations

import re
from dataclasses import replace

from pg_maker.config import environment_requirements
from pg_maker.errors import CancelledError, PreconditionError, ValidationError
from pg_maker.models import CleanupPolicy, CommandSpec, EnvironmentSpec
from pg_maker.planning.documents import seal
from pg_maker.runners.docker import DockerRunner
from pg_maker.storage import content_hash, parse_json, read_json, write_json


def missing_packages(requirements: list[str], inventory: list[dict]) -> list[str]:
    missing = []
    for requirement in requirements:
        package, _, version = requirement.partition("=")
        name, _, architecture = package.partition(":")
        if not any(
            item["name"] == name
            and (not version or item["version"] == version)
            and (not architecture or item["architecture"] in {architecture, "all"})
            for item in inventory
        ):
            missing.append(requirement)
    return missing


def prepare_environments(config: dict, context) -> dict:
    requirements = environment_requirements(config)
    runner = DockerRunner(
        context.directory,
        operation_id=context.state["operation_id"],
        owner_token=context.state["owner_token"],
        cancelled=context.token.requested,
        on_process=context.on_process,
        attempt=context.state["attempt"],
    )
    runner.doctor()
    context.event(
        "preparation_planned", progress={"completed": 0, "total": len(requirements["environments"])}
    )
    context.progress.configure(len(requirements["environments"]))
    images = []
    partial_path = context.directory / "prepared-images.json"
    saved = read_json(partial_path) if partial_path.is_file() else {}
    for requirement in requirements["environments"]:
        context.check()
        previous = saved.get(requirement["id"])
        if previous and previous["requirement_hash"] == content_hash(requirement):
            record = previous["image"]
            exists = runner._control(
                ["image", "inspect", record["image_id"], "--format", "{{.Id}}"],
                check=False,
            )
            if exists.succeeded and exists.stdout_tail.strip() == record["image_id"]:
                images.append(record)
                context.advance("environment_reused", target_id=requirement["target"])
                continue
        image = requirement["image"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:@-]*", image):
            raise ValidationError("invalid base image reference")
        local = runner._control(["image", "inspect", image, "--format", "{{.Id}}"], check=False)
        if not local.succeeded:
            if context.offline or image.startswith("sha256:"):
                raise PreconditionError(
                    "base image is not available locally", details={"image": image}
                )
            context.command(
                [
                    "docker",
                    "pull",
                    "--platform",
                    "linux/amd64" if requirement["architecture"] == "x86_64" else "linux/arm64",
                    image,
                ]
            )
        inspected = parse_json(
            runner._control(
                [
                    "image",
                    "inspect",
                    image,
                    "--format",
                    '{"Id":{{json .Id}},"Os":{{json .Os}},"Architecture":{{json .Architecture}},'
                    '"RepoDigests":{{json .RepoDigests}}}',
                ]
            ).stdout_tail
        )
        base_id = inspected["Id"]
        expected_arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(
            requirement["architecture"], requirement["architecture"]
        )
        if inspected["Os"] != "linux" or inspected["Architecture"] != expected_arch:
            raise PreconditionError("base image architecture differs from target")
        handle = runner.intended_handle(EnvironmentSpec(base_id, "0:0", requirement["id"]))
        context.event(
            "environment_preparing",
            resources=[
                {
                    "kind": "docker",
                    "name": handle.name,
                    "labels": handle.labels,
                    "container_id": None,
                    "environment": {
                        "image": base_id,
                        "user": "0:0",
                        "target_id": requirement["id"],
                    },
                }
            ],
        )
        try:
            command = [
                "create",
                "--pull=never",
                "--name",
                handle.name,
                "--user",
                "0:0",
                "--memory",
                str(config["defaults"]["execution"]["limits"]["memory_bytes"]),
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "sleep",
            ]
            for key, val in handle.labels.items():
                command += ["--label", key + "=" + val]
            if context.offline:
                command += ["--network", "none"]
            handle = replace(
                handle,
                container_id=runner._control([*command, base_id, "infinity"]).stdout_tail.strip(),
            )
            context.event(
                "environment_created",
                resources=[
                    {
                        "kind": "docker",
                        "name": handle.name,
                        "labels": handle.labels,
                        "container_id": handle.container_id,
                        "environment": {
                            "image": base_id,
                            "user": "0:0",
                            "target_id": requirement["id"],
                        },
                    }
                ],
            )
            runner._control(["start", handle.container_id])
            counter = 0

            def execute(argv: list[str], *, timeout=1800, handle=handle, requirement=requirement):
                nonlocal counter
                context.check()
                counter += 1
                result = runner.execute(
                    handle,
                    CommandSpec(
                        requirement["id"] + "." + str(counter),
                        tuple(argv),
                        cwd="/",
                        env={"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"},
                        timeout_seconds=timeout,
                    ),
                )
                if result.cancelled:
                    raise CancelledError("environment preparation cancelled")
                if not result.succeeded:
                    raise PreconditionError(
                        "environment command failed",
                        details={
                            "command": argv[0],
                            "stderr": result.stderr_tail,
                            "returncode": result.returncode,
                        },
                    )
                return result

            os_release = execute(["cat", "/etc/os-release"], timeout=30).stdout_tail
            values = dict(
                re.findall(r'^([A-Z_]+)=["\']?([^"\'\n]+)["\']?$', os_release, re.MULTILINE)
            )
            if (
                values.get("ID") != requirement["os"]["id"]
                or values.get("VERSION_ID") != requirement["os"]["version"]
            ):
                raise PreconditionError("base image OS differs from target")
            if not context.offline:
                execute(["apt-get", "update"])
                execute(
                    [
                        "apt-get",
                        "install",
                        "-y",
                        "--no-install-recommends",
                        *requirement["packages"],
                    ]
                )
            # Offline preparation only verifies a previously provisioned image.
            package_result = execute(
                [
                    "dpkg-query",
                    "-W",
                    "-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\\n",
                ]
            )
            from pathlib import Path

            inventory = []
            for line in Path(package_result.stdout_log).read_text().splitlines():
                name, version, arch, status = line.split("\t")
                if status == "installed":
                    inventory.append({"name": name, "version": version, "architecture": arch})
            missing = missing_packages(requirement["packages"], inventory)
            if missing:
                raise PreconditionError(
                    "image lacks required packages", details={"missing": missing}
                )
            for locale in requirement["locales"]:
                if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}\.UTF-8", locale):
                    raise ValidationError("database locale must use ll_CC.UTF-8")
                execute(["localedef", "-i", locale.split(".")[0], "-f", "UTF-8", locale])
            uid, gid = requirements["builder_user"].split(":")
            account = runner.execute(
                handle,
                CommandSpec(
                    requirement["id"] + ".user",
                    ("getent", "passwd", uid),
                    cwd="/",
                    timeout_seconds=30,
                ),
            )
            context.check()
            if account.returncode == 2:
                group = runner.execute(
                    handle,
                    CommandSpec(
                        requirement["id"] + ".group",
                        ("getent", "group", gid),
                        cwd="/",
                        timeout_seconds=30,
                    ),
                )
                if group.returncode == 2:
                    execute(["groupadd", "--gid", gid, "pgmaker" + gid])
                elif not group.succeeded:
                    raise PreconditionError("cannot verify build group")
                execute(
                    [
                        "useradd",
                        "--uid",
                        uid,
                        "--gid",
                        gid,
                        "--no-create-home",
                        "--home-dir",
                        "/work/build",
                        "pgmaker" + uid,
                    ]
                )
            elif not account.succeeded:
                raise PreconditionError("cannot verify build user")
            tools = {
                name: execute(argv, timeout=30).stdout_tail.strip()
                for name, argv in requirement["tools"].items()
            }
            from pg_maker.postgresql import verify_tools

            verify_tools(requirement.get("tool_minimums", {}), tools)
            from pg_maker.planning.runtime_repository import prepare_repository

            repository = prepare_repository(
                execute,
                requirement["runtime_packages"],
                offline=context.offline,
            )
            execute(["apt-get", "clean"])
            runner.cancel(handle)
            image_id = context.command(
                ["docker", "commit", handle.container_id]
            ).stdout_tail.strip()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise PreconditionError("Docker commit did not return an image ID")
            images.append(
                {
                    "id": requirement["id"],
                    "target": requirement["target"],
                    "role": requirement["role"],
                    "image_id": image_id,
                    "base_image_id": base_id,
                    "base_registry_digests": sorted(inspected.get("RepoDigests") or []),
                    "registry_manifest_digest": None,
                    "image_archive_sha256": None,
                    "os": requirement["os"],
                    "architecture": requirement["architecture"],
                    "packages": sorted(inventory, key=lambda p: p["name"]),
                    "tools": tools,
                    "method": "apt-commit",
                    "reproducibility": "local image pinned; apt repositories are mutable",
                    "runtime_repository": repository,
                }
            )
            saved[requirement["id"]] = {
                "requirement_hash": content_hash(requirement),
                "image": images[-1],
            }
            write_json(partial_path, saved)
            context.advance("environment_prepared", target_id=requirement["target"])
        finally:
            # Keep only the committed images; construction containers are owned operation resources.
            runner.release(handle, CleanupPolicy.REMOVE)
            context.event("environment_released", resources=[])
    return seal("environment", {"requirements_hash": content_hash(requirements), "images": images})
