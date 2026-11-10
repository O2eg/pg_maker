"""Durable standalone extension preparation and execution on shared worker primitives."""

from __future__ import annotations

import ast
import os
import shutil
import time
import uuid
from dataclasses import asdict
from pathlib import Path

from packaging.specifiers import SpecifierSet

from pg_maker.config import resource_path
from pg_maker.errors import CancelledError, CommandError, MakerError, PreconditionError
from pg_maker.execution.cancellation import process_identity
from pg_maker.execution.preparation import PreparationContext
from pg_maker.execution.state import OperationStore
from pg_maker.extensions.config import order_extensions, plan, seal
from pg_maker.extensions.container import migration_modules
from pg_maker.models import CleanupPolicy, CommandSpec, EnvironmentSpec, parse_request
from pg_maker.planning.documents import implementation_identity
from pg_maker.progress import ProgressReporter
from pg_maker.runners.docker import DockerRunner
from pg_maker.sources.patches import patch_component_source
from pg_maker.sources.resolver import SourceResolver
from pg_maker.sources.store import InputStore
from pg_maker.storage import content_hash, file_hash, read_json, write_json
from pg_maker.validation.diagnostics import classify
from pg_maker.validation.packages import INSTALL_CAPABILITIES


def preparation_identity(source):
    """Hash provisioning and its helper closure, excluding compilation/test-only edits."""
    module = ast.parse(source)
    functions = {n.name: n for n in module.body if isinstance(n, ast.FunctionDef)}
    pending, selected = ["provision", "inspect_target"], {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        node = functions[name]
        selected[name] = ast.dump(node, include_attributes=False)
        pending.extend(
            n.id
            for n in ast.walk(node)
            if isinstance(n, ast.Name) and n.id in functions and n.id not in selected
        )
    globals_ = [
        ast.dump(n, include_attributes=False)
        for n in module.body
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef))
    ]
    return content_hash({"functions": selected, "globals": globals_})


def environment_key(config, build_dependencies, runtime_dependencies, uid):
    return content_hash(
        {
            "target": config["target"],
            "build": build_dependencies,
            "runtime": runtime_dependencies,
            "migration_modules": migration_modules(config),
            "uid": uid,
            "helper": preparation_identity(resource_path("extensions", "container.py").read_text()),
        }
    )


class Session:
    def __init__(self, context, image, config, *, online=False, workspace=None):
        self.context, self.image, self.config = context, image, config
        self.online, self.handle, self.count = online, None, 0
        self.session_id = uuid.uuid4().hex[:12]
        self.runner = DockerRunner(
            context.directory,
            operation_id=context.directory.name,
            owner_token=context.state["owner_token"],
            cancelled=context.token.requested,
            on_process=context.on_process,
            attempt=context.state["attempt"],
            workspace=workspace,
        )

    def __enter__(self):
        environment = EnvironmentSpec(self.image, "0:0", "extensions")
        intended = self.runner.intended_handle(environment)
        self.context.event(
            "environment_preparing",
            resources=[
                {
                    "kind": "docker",
                    "name": intended.name,
                    "labels": intended.labels,
                    "container_id": None,
                    "environment": asdict(environment),
                }
            ],
        )
        self.handle = intended
        try:
            self.handle = self.runner.prepare(
                environment,
                capabilities=INSTALL_CAPABILITIES
                + (("SETFCAP",) if self.config["target"]["format"] == "rpm" else ()),
                memory_bytes=self.config["execution"]["memory_bytes"],
                cpu_jobs=self.config["execution"]["jobs"],
                network_access=self.online,
            )
            self.context.event(
                "environment_ready",
                resources=[
                    {
                        "kind": "docker",
                        "name": self.handle.name,
                        "labels": self.handle.labels,
                        "container_id": self.handle.container_id,
                        "environment": asdict(environment),
                    }
                ],
            )
        except BaseException:
            self.runner.release(self.handle, CleanupPolicy.REMOVE)
            raise
        return self

    def __exit__(self, *exc):
        try:
            # Native package tools run as root. Retained scratch must remain removable
            # by its host owner after failure, cancellation or keep_workspace=always.
            owned = self.runner._inspect(self.handle)
            if owned:
                if not owned["running"]:
                    # Its entrypoint is our idle sleep process, never a build command.
                    self.runner._control(["start", self.handle.container_id])
                self.runner._control(
                    [
                        "exec",
                        self.handle.container_id,
                        "chown",
                        "-R",
                        str(os.getuid()),
                        "/work/build",
                        "/work/output",
                    ],
                    timeout=120,
                )
        finally:
            self.runner.release(self.handle, CleanupPolicy.REMOVE)
            self.context.event("environment_released", resources=[])

    def command(self, argv, *, stage="prepare", test=False, check=True, cwd="/"):
        self.context.check()
        if (
            shutil.disk_usage(self.context.directory).free
            < self.config["execution"]["min_free_disk_bytes"]
        ):
            raise PreconditionError("extension operation reached its free disk reserve")
        self.count += 1
        self.context.event("extension_stage_started", step_id=stage)
        result = self.runner.execute(
            self.handle,
            CommandSpec(
                f"extensions-{stage}-{self.session_id}-{self.count}",
                tuple(argv),
                cwd=cwd,
                env={"LC_ALL": "C.UTF-8", "DEBIAN_FRONTEND": "noninteractive"},
                secret_env={
                    name: name
                    for name in (
                        "http_proxy",
                        "https_proxy",
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "no_proxy",
                        "NO_PROXY",
                    )
                    if self.online and os.environ.get(name)
                },
                timeout_seconds=self.config["execution"]["stage_timeout_seconds"],
            ),
        )
        self.context.check()
        if check:
            diagnostic = classify(
                Path(result.stdout_log),
                Path(result.stderr_log),
                stage=stage,
                tool=argv[0],
                test_command=test,
            )
            path = (
                self.context.directory
                / "workspace/output/diagnostics"
                / (stage + f"-{self.session_id}-{self.count}.json")
            )
            write_json(path, diagnostic)
            if not result.succeeded or not diagnostic["passed"]:
                raise CommandError(
                    "extension stage failed",
                    details={
                        "stage": stage,
                        "returncode": result.returncode,
                        "stdout": result.stdout_log,
                        "stderr": result.stderr_log,
                        "diagnostics": str(path),
                    },
                )
        return result

    def helper(self, action, job, *, test=False):
        root = self.runner.workspace / "source"
        self.count += 1
        name = f"job-{self.count}.json"
        write_json(root / name, job)
        return self.command(
            ["python3", "/work/source/extension-container.py", action, "/work/source/" + name],
            stage=action,
            test=test,
        )


def setup_workspace(context):
    root = context.directory / "workspace"
    for name in ("source", "build", "output"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        resource_path("extensions", "container.py"), root / "source/extension-container.py"
    )
    return root


def prepare(request, context):
    import copy

    config = copy.deepcopy(request.config)
    root = setup_workspace(context)
    store = InputStore(Path(request.input_store))
    resolver = SourceResolver(
        store,
        context.directory / "resolution",
        offline=request.offline,
        cancelled=context.token.requested,
        on_process=context.on_process,
    )
    for component in config["extensions"]:
        spec, base = component["spec"], Path(component.pop("base"))
        spec["source"] = resolver.resolve(spec["source"], base)
        patches = []
        for index, spelling in enumerate(spec.get("source_patches", [])):
            path = base / spelling
            digest = store.put_blob(path, file_hash(path))
            patches.append({"input": f"patch-{index}", "sha256": digest})
        spec["source_patches"] = patches
        patch_component_source(spec, store, context.directory / "resolution", check=context.check)
        context.advance("extension_source_resolved", step_id=spec["sql_extension"])
    package_dir = root / "source/database-packages"
    package_dir.mkdir(exist_ok=True)
    for item in config["target"]["packages"]:
        digest = store.put_blob(Path(item["path"]), item["sha256"])
        destination = package_dir / Path(item["path"]).name
        if destination.exists() and file_hash(destination) != digest:
            raise PreconditionError("database packages have conflicting filenames")
        shutil.copy2(store.blob(digest), destination)
        item["filename"] = destination.name
        item.pop("path")
    runtime_dependencies = sorted(
        {d for c in config["extensions"] for d in c["spec"]["runtime_dependencies"]}
    )
    build_dependencies = sorted(
        {
            d
            for c in config["extensions"]
            for d in c["spec"]["build_dependencies"]
            + (c["spec"].get("test_dependencies", []) if config["validation"]["run_tests"] else [])
        }
    )
    job = {
        "config": config,
        "uid": os.getuid() or 1000,
        "runtime_dependencies": runtime_dependencies,
        "build_dependencies": build_dependencies,
    }
    cache_key = environment_key(config, build_dependencies, runtime_dependencies, job["uid"])
    cache = Path(request.input_store) / "extension-environments" / (cache_key[7:] + ".json")
    runner = DockerRunner(
        context.directory,
        operation_id=context.directory.name,
        owner_token=context.state["owner_token"],
        cancelled=context.token.requested,
        on_process=context.on_process,
        attempt=context.state["attempt"],
    )
    runner.doctor()
    prepared = None
    if cache.is_file():
        candidate = read_json(cache)
        if candidate.get("cache_key") == cache_key and candidate.get("record_hash") == content_hash(
            candidate["record"]
        ):
            if all(
                runner._control(
                    ["image", "inspect", image, "--format", "{{.Id}}"], check=False
                ).succeeded
                for image in candidate["record"]["images"].values()
            ):
                prepared = candidate["record"]
    if prepared is None or set(prepared["images"]) != {"build", "test"}:
        if request.offline:
            raise PreconditionError("pinned extension environments are unavailable offline")
        target = config["target"]
        image = target["base_image"]
        probe = runner._control(["image", "inspect", image, "--format", "{{.Id}}"], check=False)
        if not probe.succeeded:
            runner._control(
                ["pull", "--platform", "linux/" + target["architecture"], image], timeout=1800
            )
        base = runner._control(["image", "inspect", image, "--format", "{{.Id}} {{.Architecture}}"])
        base_id, arch = base.stdout_tail.strip().split()
        if arch != target["architecture"]:
            raise PreconditionError("image architecture differs from extension target")
        prepared = prepared or {"images": {}, "base_image": base_id}
        for role in ("build", "test"):
            if role in prepared["images"]:
                continue
            with Session(context, base_id, config, online=True) as session:
                session.command(
                    [
                        "sh",
                        "-ec",
                        "command -v python3 >/dev/null || "
                        + (
                            "(apt-get update && apt-get -y install python3)"
                            if target["format"] == "deb"
                            else "dnf -y install python3"
                        ),
                    ],
                    stage="bootstrap",
                )
                session.helper("provision-" + role, job)
                if role == "build":
                    session.helper("inspect", job)
                    prepared["target"] = read_json(root / "output/target.json")
                    session.runner._control(
                        [
                            "cp",
                            session.handle.container_id
                            + ":/opt/pg-maker/extension-runtime-repository.json",
                            str(root / "output/runtime-repository.json"),
                        ]
                    )
                    prepared["runtime_repository"] = read_json(
                        root / "output/runtime-repository.json"
                    )
                    if (root / "output/target-packages/migration.json").is_file():
                        prepared["migration"] = read_json(
                            root / "output/target-packages/migration.json"
                        )
                        for record in prepared["migration"]:
                            store.put_blob(
                                root / "output/target-packages" / record["filename"],
                                record["sha256"],
                            )
                image_result = session.runner._control(
                    ["commit", session.handle.container_id], timeout=1800
                )
                prepared["images"][role] = image_result.stdout_tail.strip()
            context.advance("extension_environment_prepared", step_id=role)
            write_json(
                cache,
                {"cache_key": cache_key, "record": prepared, "record_hash": content_hash(prepared)},
            )
    else:
        context.advance("extension_environments_reused")
    version = prepared["target"]["postgresql_version"]
    config["extensions"] = order_extensions(config, prepared["target"])
    for component in config["extensions"]:
        if version not in SpecifierSet(component["spec"]["compatible_postgresql"]):
            raise PreconditionError("extension recipe does not support target PostgreSQL version")
    result = seal(
        "lock",
        {
            "config": config,
            **prepared,
            "uid": job["uid"],
            "implementation": implementation_identity(),
        },
    )
    result["transport"] = {"input_store": request.input_store}
    return result


def build(request, context):
    from pg_maker.extensions.packages import build_extensions

    plan(request.lock)
    return build_extensions(request, context)


def execute_extensions(directory, token):
    state = OperationStore(directory).load()
    error, result, status = None, None, "succeeded"
    with ProgressReporter(
        directory / "attempts" / str(state["attempt"]) / "progress.log",
        operation_id=directory.name,
        attempt=state["attempt"],
    ) as progress:
        context = PreparationContext(directory, token, progress)
        try:
            payload = read_json(directory / "input.json")
            if content_hash(payload) != state["input_hash"]:
                raise PreconditionError("extension operation input changed")
            request = parse_request(payload)
            if (
                shutil.disk_usage(directory).free
                < request.config["execution"]["min_free_disk_bytes"]
            ):
                raise PreconditionError("insufficient free disk space for extension operation")
            context.event(
                "operation_started", status="running", owner=process_identity(os.getpid())
            )
            progress.configure(1)
            started = time.monotonic()
            document = (
                prepare(request, context)
                if request.operation_kind == "extension_prepare"
                else build(request, context)
            )
            write_json(Path(request.output), document)
            result = {
                "document": request.output,
                "sha256": file_hash(Path(request.output)),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "qualification": document.get("qualification", "unverified"),
            }
        except MakerError as exc:
            error, status = exc, "cancelled" if isinstance(exc, CancelledError) else "failed"
        except Exception as exc:
            error, status = MakerError(f"{type(exc).__name__}: {exc}"), "failed"
        context.event(
            "operation_finished",
            status=status,
            owner=None,
            result=result,
            resources=[r for r in context.state["resources"] if r["kind"] != "process"],
            error=error.as_dict() if error else None,
        )
        if error:
            progress.error(str(error))
        else:
            progress.info("Extension operation completed")
    return error.exit_code if error else 0
