"""Run installed-product checks in fresh, network-isolated test environments."""

import shutil
import sys
from dataclasses import asdict

from pg_maker.config import resource_path
from pg_maker.errors import CancelledError, CommandError, PreconditionError
from pg_maker.models import CleanupPolicy, CommandSpec, EnvironmentSpec
from pg_maker.packaging.deb import inspect_deb
from pg_maker.packaging.service import settings
from pg_maker.planning.runtime_repository import verify_repository
from pg_maker.runners.docker import DockerRunner
from pg_maker.storage import content_hash, ensure_directory, file_hash, read_json, write_json
from pg_maker.yamlio import load_yaml

INSTALL_CAPABILITIES = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID", "KILL")
SERVER_CHECKS = {
    "missing-internal-dependency-rejected",
    "installed-inventory-and-loader",
    "install-does-not-initdb",
    "empty-pgdata-start-rejected",
    "service-start-status-reload",
    "socket-and-tcp",
    "compiled-socket-default",
    "installed-server-version",
    "restart-preserves-data",
    "dump-restore",
    "crash-recovery",
    "checksums",
    "invalid-config-permissions-port-socket-rejected",
    "reinstall-preserves-config-data",
    "config-and-file-logs-in-pgdata",
    "remove-preserves-data",
    "install-after-remove",
    "purge-preserves-data",
}


def runtime_repository(branch):
    image = next(
        i
        for i in branch.request.lock["spec"]["environments"]["spec"]["images"]
        if i["id"] == branch.variant["target"] + ".build"
    )
    repository = image.get("runtime_repository")
    if not repository or repository["repository_hash"] != content_hash(repository["files"]):
        raise PreconditionError(
            "prepared build image has no pinned runtime repository; run env prepare"
        )
    destination = branch.output / "runtime-repository"
    if not destination.exists():
        branch.prepare()
        branch.runner._control(
            ["cp", branch.handle.container_id + ":" + repository["path"], str(destination)]
        )
    # APT's empty scratch entries are not part of its package inventory.
    for name in ("partial", "lock"):
        path = destination / name
        if path.is_symlink():
            raise PreconditionError("runtime repository contains a symlink")
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
        elif path.is_file() and not path.stat().st_size:
            path.unlink()
    verify_repository(destination, repository)
    write_json(branch.output / "runtime-repository.json", repository)
    return destination


def validate_packages(branch, *, run_database=True):
    repository = runtime_repository(branch)
    package_list = read_json(branch.output / "packages.json")
    for package in package_list:
        if file_hash(branch.output / package["path"]) != package["sha256"]:
            raise PreconditionError("package changed before installation")
    tree = read_json(branch.output / "install-tree.json")
    previous = next(
        (
            r
            for r in branch.config.get("previous_releases", [])
            if r["variant"] == branch.variant["id"] and branch.branch == "candidate"
        ),
        None,
    )
    previous_packages = []
    if previous:
        for item in previous["packages"]:
            path = branch.inputs.blob(item["sha256"])
            metadata = inspect_deb(path)["control"]
            current = next(p for p in package_list if p["role"] == item["role"])
            if (
                metadata.get("Package") != current["name"]
                or metadata.get("Architecture") != current["native_arch"]
            ):
                raise PreconditionError("previous package identity differs from the upgrade target")
            previous_packages.append(
                {**item, "version": metadata["Version"], "filename": item["role"] + ".deb"}
            )
    branch.release()
    acceptance, seen = [], set()
    for change in branch.feature["spec"]["changes"]:
        for test in change["spec"]["acceptance"]:
            if branch.branch not in test["expectations"]:
                continue
            if test["id"] in seen:
                continue
            seen.add(test["id"])
            blob = branch.inputs.blob(test["script"]["sha256"])
            item = dict(test)
            if test["kind"] == "command":
                document = load_yaml(blob)
                if set(document) != {"argv"} or not isinstance(document["argv"], list):
                    raise PreconditionError("invalid command acceptance script")
                item["argv"] = document["argv"]
            else:
                item["sql"] = blob.read_text()
            acceptance.append(item)
    outcomes = []
    scenarios = [
        ("libpq", ("libpq",)),
        ("client", ("libpq", "client")),
        ("server", ("libpq", "client", "server")),
    ]
    documentation = branch.config.get("documentation", {}).get("enabled", False)
    if documentation:
        scenarios[-1] = ("server", ("libpq", "client", "server", "docs"))
        scenarios.append(("docs", ("docs",)))
    if previous and run_database:
        scenarios.append(("upgrade", ("libpq", "client", "server")))
    for label, roles in scenarios:
        branch.context.check()
        workspace = (
            branch.context.directory
            / "validation"
            / branch.id
            / str(branch.context.state["attempt"])
            / label
        )
        ensure_directory(workspace)
        runner = DockerRunner(
            branch.context.directory,
            operation_id=branch.context.directory.name,
            owner_token=branch.context.state["owner_token"],
            cancelled=branch.context.token.requested,
            on_process=branch.context.on_process,
            attempt=branch.context.state["attempt"],
            workspace=workspace,
        )
        source = workspace / "source"
        ensure_directory(source)
        shutil.copytree(branch.output / "packages", source / "packages")
        verify_repository(repository, read_json(branch.output / "runtime-repository.json"))
        shutil.copytree(repository, source / "repository", symlinks=True)
        if label == "upgrade":
            ensure_directory(source / "previous")
            for item in previous_packages:
                shutil.copyfile(
                    branch.inputs.blob(item["sha256"]), source / "previous" / item["filename"]
                )
        for name in ("systemctl.py", "installed.py"):
            shutil.copyfile(resource_path("validation", name), source / name)
        if documentation:
            shutil.copytree(
                resource_path(),
                source / "doc-tools/pg_maker",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        config = {
            "service": settings(branch.config, branch.variant),
            "roles": roles,
            "packages": package_list,
            "inventory": tree["entries"],
            "branch": branch.branch,
            "postgresql_version": branch.config["postgresql"]["version"],
            "components": [
                {"id": c["metadata"]["id"], **c["spec"]}
                for c in branch.feature["spec"]["components"]
            ],
            "acceptance": acceptance,
            "run_database": run_database,
            "previous_declared": previous is not None,
            "previous": {**previous, "packages": previous_packages} if label == "upgrade" else None,
            "documentation": read_json(branch.output / "installed-docs.json")
            if documentation
            else None,
            "documentation_root": branch.variant["identity"]["paths"]["documentation_directory"],
            "documentation_branding": branch.config["documentation"]["branding"],
        }
        write_json(source / "config.json", config)
        environment = EnvironmentSpec(
            branch.step["image_id"], "0:0", branch.id + ".packages." + label
        )
        handle = runner.intended_handle(environment)

        def record(handle):
            branch.context.event(
                "environment_recorded",
                resources=[
                    {
                        "kind": "docker",
                        "name": handle.name,
                        "labels": handle.labels,
                        "container_id": handle.container_id,
                        "environment": asdict(handle.environment),
                    }
                ],
            )

        record(handle)
        try:
            handle = runner.prepare(
                environment,
                memory_bytes=branch.step["resources"]["memory_bytes"],
                cpu_jobs=branch.step["resources"]["cpu_jobs"],
                capabilities=INSTALL_CAPABILITIES,
            )
            record(handle)
            command = CommandSpec(
                branch.id + ".packages." + label,
                (
                    "python3",
                    "/work/source/installed.py",
                    "/work/source/config.json",
                    "/tmp/pg-maker-results",
                ),
                cwd="/",
                timeout_seconds=branch.step["timeout_seconds"],
            )
            result = runner.execute(handle, command)
            output = branch.output / "package-tests" / label
            ensure_directory(output.parent)
            copied = runner._control(
                ["cp", handle.container_id + ":/tmp/pg-maker-results", str(output)],
                check=False,
            )
            if result.cancelled:
                raise CancelledError("package validation cancelled")
            if (
                not result.succeeded
                or not copied.succeeded
                or not (output / "result.json").is_file()
            ):
                raise CommandError(
                    "installed package checks failed",
                    details={
                        "set": label,
                        "stderr": result.stderr_tail,
                        "timed_out": result.timed_out,
                    },
                )
            report = read_json(output / "result.json")
            required = (
                {"previous-release-upgrade"}
                if label == "upgrade"
                else SERVER_CHECKS
                if label == "server" and run_database
                else {
                    "installed-inventory-and-loader",
                    "install-does-not-initdb",
                    "empty-pgdata-start-rejected",
                    "remove-preserves-empty-pgdata",
                }
                if label == "server"
                else {
                    "installed-inventory-and-loader",
                    "no-cluster-or-service",
                    "standalone-remove",
                }
            )
            passed = {c["id"] for c in report["checks"] if c["status"] == "passed"}
            if documentation and label != "upgrade":
                required = required | {"installed-documentation"}
            if not report["passed"] or not required <= passed:
                raise CommandError("installed check evidence is incomplete")
            if (
                label == "server"
                and run_database
                and not {"acceptance:" + t["id"] for t in acceptance} <= passed
            ):
                raise CommandError("installed acceptance evidence is incomplete")
            outcomes.append(
                {
                    "set": label,
                    "passed": True,
                    "checks": sorted(passed),
                    "path": "package-tests/" + label + "/result.json",
                    "sha256": file_hash(output / "result.json"),
                }
            )
        finally:
            original_error = sys.exc_info()[1]
            try:
                runner.release(handle, CleanupPolicy.REMOVE)
                branch.context.event("environment_released", resources=[])
            except Exception:
                if original_error is None:
                    raise
                # Keep the exact refs for cancel/resume without hiding the original test error.
                branch.context.event("environment_cleanup_failed")
    result = {
        "passed": True,
        "sets": outcomes,
        "run_database": run_database,
        "service_manager": "emulated",
    }
    write_json(branch.output / "package-tests.json", result)
    branch.report["package_tests"] = result
    return result
