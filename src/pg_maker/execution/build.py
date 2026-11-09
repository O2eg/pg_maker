"""Sequential locked build DAG executor using the durable operation/runner contracts."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict
from pathlib import Path

from pg_maker.artifacts.manifest import complete_matrix, render_report
from pg_maker.builders import autotools, components
from pg_maker.changes.apply import apply_changes
from pg_maker.config import resource_path
from pg_maker.errors import CancelledError, CommandError, MakerError, PreconditionError
from pg_maker.execution.cancellation import process_identity
from pg_maker.execution.checkpoints import Checkpoints
from pg_maker.execution.preparation import PreparationContext
from pg_maker.execution.resources import check_resources
from pg_maker.execution.state import OperationStore
from pg_maker.models import CleanupPolicy, CommandSpec, EnvironmentSpec, parse_request
from pg_maker.planning.documents import verify
from pg_maker.planning.plan import build_plan
from pg_maker.progress import ProgressReporter
from pg_maker.runners.docker import DockerRunner, current_user
from pg_maker.sources.store import InputStore, inventory
from pg_maker.storage import content_hash, ensure_directory, file_hash, read_json, write_json


class BuildBranch:
    def __init__(self, context, runner, request, step):
        self.context, self.runner, self.request = context, runner, request
        self.config = request.lock["spec"]["config"]
        self.variant = next(
            v
            for v in self.config["variants"]
            if v["feature"] == step["feature_id"] and v["target"] == step["target_id"]
        )
        self.feature = next(
            f for f in self.config["features"] if f["metadata"]["id"] == step["feature_id"]
        )
        self.branch = step["branch"]
        self.id = self.variant["id"] + "." + self.branch
        self.root = context.directory / "workspace/build" / self.id
        self.container_root = "/work/build/" + self.id
        self.source, self.obj = self.root / "source", self.root / "obj"
        self.output = context.directory / "workspace/output" / self.id
        self.helper = context.directory / "workspace/source"
        self.prefix = self.variant["identity"]["paths"]["runtime_prefix"]
        self.inputs = InputStore(Path(request.input_store))
        self.handle = None
        self.counter = 0
        self.report = {
            "api_version": "pg_maker/build-report/v1",
            "variant": self.variant["id"],
            "branch": self.branch,
            "lock_hash": request.lock["lock_hash"],
            "plan_hash": request.plan["plan_hash"],
            "qualification": "unverified",
            "db_tests": "pending" if request.plan["spec"]["run_db_tests"] else "skipped",
            "publication_eligible": False,
            "steps": [],
        }
        ensure_directory(self.helper)
        (self.helper / "inventory.mk").write_text(autotools.INVENTORY_MAKE)
        ensure_directory(self.helper / "PgMaker")
        (self.helper / "PgMaker/Formatter.pm").write_text(autotools.TAP_FORMATTER)
        shutil.copytree(
            resource_path("validation", "upstream"),
            self.helper / "upstream-tests",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        self.step = step

    def begin(self, *, complete_checkpoint=False):
        cache_producer = self.report.get("cache_producer")
        for path in (self.root, self.obj, self.output):
            ensure_directory(path)
        if (self.output / "report.json").is_file():
            self.report = read_json(self.output / "report.json")
        if cache_producer:
            self.report["cache_producer"] = cache_producer
        if "build_prefix" in self.report:
            self.report["build_prefix"] = str(self.root / "prefix")
        self.report.update(
            lock_hash=self.request.lock["lock_hash"], plan_hash=self.request.plan["plan_hash"]
        )
        if complete_checkpoint:
            # Recovery after the final durable snapshot only finishes the operation.
            # Its verified package/test records must survive finalization.
            return
        component_ids = {c["metadata"]["id"] for c in self.feature["spec"]["components"]}
        self.report["components"] = [
            c for c in self.report.get("components", []) if c["id"] in component_ids
        ]
        self.report.update(
            status="running",
            qualification="unverified",
            publication_eligible=False,
            db_tests="pending" if self.request.plan["spec"]["run_db_tests"] else "skipped",
            steps=[],
        )
        self.report.pop("error", None)
        for name in (
            "artifact_manifest",
            "verification",
            "packages",
            "install_tree",
            "package_tests",
            "verified_test_ids",
            "test_policy",
            "source_test_scope",
            "core_tests",
            "world_tests",
        ):
            self.report.pop(name, None)
        self.counter = max(
            (int(p.stem) for p in (self.output / "commands").glob("*.json")), default=0
        )
        write_json(self.output / "report.json", self.report)

    def prepare(self):
        if self.handle is not None:
            return
        environment = EnvironmentSpec(self.step["image_id"], self.step["user"], self.id)
        self.handle = self.runner.intended_handle(environment)
        self.record_resource()
        self.handle = self.runner.prepare(
            environment,
            memory_bytes=self.step["resources"]["memory_bytes"],
            cpu_jobs=self.step["resources"]["cpu_jobs"],
            prefix_mount=(self.root / "prefix", self.prefix),
            toolchain_cache=Path(self.request.input_store) / "toolchains",
        )
        self.record_resource()
        self.command(["id", "-un"])

    def record_resource(self):
        handle = self.handle
        self.context.event(
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

    def command(
        self,
        argv,
        *,
        cwd=None,
        env=None,
        timeout=None,
        accepted_exit_codes=(0,),
        test_command=False,
    ):
        self.context.check()
        self.counter += 1
        tool_env = {}
        for component in self.step.get("parameters", {}).get("components", []):
            if not isinstance(component, dict):
                continue  # Packaging stages carry selected IDs, not component recipes.
            extra = component["spec"].get("extra_sources", {})
            if {"rustc", "rust-std", "cargo", "cargo-pgrx"} <= extra.keys():
                key = content_hash(
                    {
                        "inputs": {k: v["source_hash"] for k, v in extra.items()},
                        "image": self.step["image_id"],
                        "build": self.variant["build"],
                        "postgresql": self.config["postgresql"],
                        "prefix": self.prefix,
                        "helper": file_hash(self.helper / "upstream-tests/vectorscale.py"),
                    }
                )
                tool_env["PG_MAKER_RUST_CACHE"] = "/work/cache/" + key[7:]
        command = CommandSpec(
            self.id + "." + str(self.counter),
            tuple(argv),
            cwd=cwd or self.container_root + "/obj",
            env={**self.step["env"], **tool_env, **(env or {})},
            timeout_seconds=timeout or self.step["timeout_seconds"],
        )
        result = self.runner.execute(self.handle, command)
        record = {"command": asdict(command), "result": asdict(result)}
        # Keep diagnostic references portable, including when build outputs come from cache.
        for key, suffix in (("stdout_log", "stdout.log"), ("stderr_log", "stderr.log")):
            original = Path(getattr(result, key))
            relative = "command-logs/" + str(self.counter) + "." + suffix
            ensure_directory((self.output / relative).parent)
            shutil.copyfile(original, self.output / relative)
            record["result"][key] = relative
        # Keep per-command records bounded in the journal; detailed evidence lives in report files.
        write_json(self.output / "commands" / (str(self.counter) + ".json"), record)
        from pg_maker.validation.diagnostics import record as record_diagnostics

        diagnostics = record_diagnostics(self, command, result, test_command=test_command)
        test_logs_passed = True
        if test_command:
            from pg_maker.validation.diagnostics import preserve_test_logs

            test_logs_passed = preserve_test_logs(self, command.cwd)
        self.context.check()
        if result.cancelled or result.timed_out or result.returncode not in accepted_exit_codes:
            raise CommandError(
                "build command failed",
                details={
                    "step": self.step["id"],
                    "argv": argv,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "stdout_log": result.stdout_log,
                    "stderr_log": result.stderr_log,
                    "stderr": result.stderr_tail,
                },
            )
        if not diagnostics["passed"] or not test_logs_passed:
            raise CommandError(
                "blocking command diagnostics",
                details={"step": self.step["id"], "command": command.id},
            )
        return result

    def release(self, *, keep=False):
        if self.handle is not None:
            self.runner.release(
                self.handle, CleanupPolicy.KEEP_STOPPED if keep else CleanupPolicy.REMOVE
            )
            if keep:
                return
            self.handle = None
            self.context.event("environment_released", resources=[])

    def query(self, directory):
        (self.obj / directory).mkdir(parents=True, exist_ok=True)
        result = self.command(
            [
                "make",
                "--no-print-directory",
                "-s",
                "-f",
                self.container_root + "/source/" + directory + "/Makefile",
                "-f",
                "/work/source/inventory.mk",
                "maker-inventory",
            ],
            cwd=self.container_root + "/obj/" + directory,
        )
        return dict(
            re.findall(r"^PG_MAKER_([A-Z_]+)=(.*)$", Path(result.stdout_log).read_text(), re.M)
        )

    def execute(self, step):
        self.step = step
        kind = step["kind"]
        self.context.check()
        if kind == "verify_inputs":
            self.inputs.tree(self.config["source"]["source_hash"])
        elif kind == "materialize_source":
            from pg_maker.execution.incremental import seed_core
            from pg_maker.packaging.layout import normalize_install_layout
            from pg_maker.sources.store import materialize

            source = self.inputs.tree(self.config["source"]["source_hash"])
            if not seed_core(self, source):
                materialize(source, self.source)
            self.report["installation_layout"] = normalize_install_layout(
                self.source, self.output / "install-layout.patch"
            )
        elif kind == "apply_changes":
            receipts = apply_changes(
                self.source,
                self.feature,
                self.request.lock,
                self.inputs,
                self.branch,
                self.output / "changes",
                self.context.check,
            )
            self.report["changes"] = [{k: v for k, v in r.items() if k != "diff"} for r in receipts]
        elif kind == "integrate_source_components":
            components.integrate(self, self.feature["spec"]["components"])
            self.report["source_hash"] = content_hash(inventory(self.source))
        elif kind in {"prepare_documentation", "export_documentation"} or kind.startswith(
            ("translate_docs_", "build_docs_")
        ):
            from pg_maker.documentation.builder import execute

            execute(self, step)
        elif kind == "expected_test_inventory":
            self.inventory = autotools.expected_inventory(
                self.source, self.obj, self.query, test_extra=step["env"].get("PG_TEST_EXTRA", "")
            )
            write_json(self.output / "expected-tests.json", self.inventory)
        elif kind == "build_libraries":
            self.prepare()
            components.build_components(
                self,
                step["parameters"]["components"],
                run_tests=self.request.plan["spec"]["run_db_tests"],
            )
            versions = {}
            for library in step["parameters"]["components"]:
                for module, expected in library["spec"]["pkg_config_versions"].items():
                    result = self.command(["pkg-config", "--modversion", module])
                    actual = Path(result.stdout_log).read_text().strip()
                    directory = self.command(["pkg-config", "--variable=libdir", module])
                    libdir = Path(directory.stdout_log).read_text().strip()
                    if actual != expected or libdir != self.prefix + "/lib":
                        raise PreconditionError(
                            "library version or prefix differs from recipe",
                            details={
                                "module": module,
                                "expected": expected,
                                "actual": actual,
                                "libdir": libdir,
                            },
                        )
                    versions[module] = {"version": actual, "libdir": libdir}
            self.report["library_versions"] = versions
            library_files = {
                name
                for library in step["parameters"]["components"]
                for name in read_json(
                    self.root / "install-owners" / (library["metadata"]["id"] + ".json")
                )["files"]
            }
            installed = inventory(self.root / "prefix")
            installed["entries"] = [
                entry
                for entry in installed["entries"]
                if entry["path"] in library_files
                or (
                    entry["type"] == "directory"
                    and any(name.startswith(entry["path"] + "/") for name in library_files)
                )
            ]
            write_json(self.output / "library-files.json", installed)
        elif kind in {"build_and_test_tools", "build_tools"}:
            from pg_maker.execution.component_cache import execute_component

            execute_component(
                self, step["parameters"]["components"][0], run_tests=kind == "build_and_test_tools"
            )
        elif kind == "acceptance_build_prefix":
            components.acceptance(self, self.feature["spec"]["changes"])
        elif kind in {"export_build", "verify_source"}:
            if self.request.plan["spec"]["run_db_tests"]:
                from pg_maker.validation.test_policy import (
                    source_test_policy,
                    verify_source_test_policy,
                )

                world = read_json(
                    self.output
                    / (
                        "world/world-result.json"
                        if self.branch == "candidate"
                        else "core/core-result.json"
                    )
                )
                policy = source_test_policy(
                    self.config,
                    self.variant["build"],
                    self.request.plan["spec"]["goal"],
                    self.branch,
                )
                self.report["verified_test_ids"] = autotools.verify_required_tests(
                    self.feature,
                    self.report,
                    world,
                    self.branch,
                    additional=policy["required_tests"],
                )
                self.report["test_policy"] = verify_source_test_policy(
                    self, read_json(self.output / "expected-tests.json"), world
                )
                self.report["source_test_scope"] = (
                    ("full+long" if "long" in policy["suites"] else "full")
                    if self.branch == "candidate"
                    else "baseline-core"
                )
                self.report["qualification"] = "source-verified"
                self.report["db_tests"] = "passed"
            if kind == "export_build":
                self.report["build_prefix"] = str(self.root / "prefix")
        elif kind == "collect_install_tree":
            from pg_maker.packaging.tree import collect

            collect(self)
        elif kind == "runtime_dependencies":
            from pg_maker.packaging.deb import dependencies

            dependencies(self)
        elif kind == "package":
            from pg_maker.packaging.deb import package

            package(self)
            from pg_maker.validation.packages import runtime_repository

            if (
                self.request.plan["spec"].get(
                    "run_package_tests", self.request.plan["spec"]["run_db_tests"]
                )
                or self.config["documentation"]["enabled"]
            ):
                runtime_repository(self)
        elif kind == "test_installed_packages":
            from pg_maker.validation.packages import validate_packages

            validate_packages(self)
        elif kind == "test_documentation_packages":
            from pg_maker.validation.packages import validate_packages

            validate_packages(self, run_database=False)
        elif kind == "acceptance":
            if not self.report.get("package_tests", {}).get("passed"):
                raise PreconditionError("installed acceptance has no successful evidence")
        elif kind == "export":
            from pg_maker.artifacts.manifest import finish

            finish(self)
        elif step["argv"]:
            self.prepare()
            argv, env = list(step["argv"]), dict(step["env"])
            if kind in {"test_core", "test_source_world"}:
                phase = "core" if kind == "test_core" else "world"
                tap = self.output / phase / "tap"
                from pg_maker.execution.workspace import archive_evidence

                archive_evidence(self, self.output / phase)
                ensure_directory(tap)
                env.update(
                    PERL5LIB="/work/source",
                    PG_MAKER_SOURCE=self.container_root + "/source",
                    PG_MAKER_TAP="/work/output/" + self.id + "/" + phase + "/tap",
                )
                argv.append("PROVE_FLAGS=--formatter=PgMaker::Formatter")
                # check-world covers multiple independently bounded suites, allow their aggregate.
                timeout = (
                    step["timeout_seconds"]
                    if phase == "core"
                    else max(step["timeout_seconds"], 14400)
                )
                result = self.command(argv, env=env, timeout=timeout, test_command=True)
                result = autotools.reconcile(
                    self.inventory,
                    self.obj,
                    tap,
                    core_only=phase == "core",
                    stdout=Path(result.stdout_log).read_text(),
                )
                self.report[phase + "_tests"] = {
                    "passed": result["passed"],
                    "suites": len(result["results"]),
                }
            else:
                result = self.command(argv, env=env)
                if kind == "install_build_prefix":
                    core = inventory(self.root / "prefix")
                    library_inventory = self.output / "library-files.json"
                    if library_inventory.exists():
                        original = read_json(library_inventory)["entries"]
                        installed = {e["path"]: e for e in core["entries"]}
                        if any(
                            e["type"] != "directory" and installed.get(e["path"]) != e
                            for e in original
                        ):
                            raise PreconditionError("PostgreSQL install modified a library file")
                        library_paths = {e["path"] for e in original}
                        core["entries"] = [
                            e for e in core["entries"] if e["path"] not in library_paths
                        ]
                    write_json(self.output / "core-files.json", core)
                if kind == "install_components":
                    from pg_maker.packaging.contrib import capture, enabled

                    if enabled(self.config):
                        capture(self, env)
                warnings = []
                for path in (result.stdout_log, result.stderr_log):
                    for line in Path(path).read_text(errors="replace").splitlines():
                        if re.search(r"\bwarning:", line, re.I):
                            warnings.append(line)
                if warnings:
                    write_json(self.output / "warnings" / (kind + ".json"), warnings)
                    self.report.setdefault("warnings", {})[kind] = len(warnings)
                if kind == "configure":
                    settings = (self.obj / "src/Makefile.global").read_text()
                    self.report["configure"] = autotools.verify_configure(
                        argv,
                        settings,
                        (self.obj / "src/include/pg_config.h").read_text(),
                        Path(result.stdout_log).read_text() + Path(result.stderr_log).read_text(),
                    )
        else:
            raise PreconditionError("build step has no implementation", details={"step": kind})
        self.report["steps"].append({"id": step["id"], "status": "succeeded"})
        write_json(self.output / "report.json", self.report)


def execute_build(directory, token):
    from itertools import groupby

    error, result, branch = None, None, None
    attempt = OperationStore(directory).load()["attempt"]
    with ProgressReporter(
        directory / "attempts" / str(attempt) / "progress.log",
        operation_id=directory.name,
        attempt=attempt,
    ) as progress:
        context = PreparationContext(directory, token, progress)
        runner = DockerRunner(
            directory,
            operation_id=directory.name,
            owner_token=context.state["owner_token"],
            cancelled=token.requested,
            on_process=context.on_process,
            attempt=attempt,
        )
        reports, failures, succeeded = [], [], 0
        try:
            payload = read_json(directory / "input.json")
            if content_hash(payload) != context.state["input_hash"]:
                raise PreconditionError("operation input hash changed")
            request = parse_request(payload)
            verify(request.plan, "plan")
            config = request.lock["spec"]["config"]
            autotools.validate_build(
                config,
                request.plan["spec"]["goal"],
                run_db_tests=request.plan["spec"]["run_db_tests"],
            )
            expected = build_plan(
                request.lock,
                goal=request.plan["spec"]["goal"],
                input_store=Path(request.input_store),
                run_db_tests=request.plan["spec"]["run_db_tests"],
                components=request.plan["spec"].get("components"),
            )
            if request.plan["plan_hash"] != expected["plan_hash"]:
                raise PreconditionError("operation plan differs from required build graph")
            if current_user() != config["defaults"]["execution"]["build_user"]:
                raise PreconditionError(
                    "execution.build_user must match the nonroot host uid/gid "
                    "for private bind mounts"
                )
            limits = config["defaults"]["execution"]["limits"]
            check_resources(directory, limits)
            steps = expected["spec"]["steps"]
            context.event(
                "operation_started",
                status="running",
                owner=process_identity(os.getpid()),
                progress={"completed": 0, "total": len(steps)},
            )
            progress.configure(len(steps))
            # Sequential targets respect the configured maximum without multiplying make jobs.
            for _, group in groupby(
                steps, key=lambda s: (s["feature_id"], s["target_id"], s["branch"])
            ):
                branch_steps = list(group)
                branch = BuildBranch(context, runner, request, branch_steps[0])
                identifier = branch.id
                failure = None
                try:
                    context.check()
                    from pg_maker.execution.workspace import WorkspaceJournal, compact_success

                    branch.workspace = WorkspaceJournal(branch)
                    checkpoints = Checkpoints(branch, branch_steps)
                    restored = (
                        checkpoints.restore()
                        if (not branch.workspace.reused and any(checkpoints.index.glob("*.json")))
                        else -1
                    )
                    if restored >= 0 and restored != len(branch_steps) - 1:
                        branch.workspace = WorkspaceJournal(branch)
                    branch.begin(complete_checkpoint=restored == len(branch_steps) - 1)
                    for index, step in enumerate(branch_steps):
                        context.check()
                        if index <= restored and not checkpoints.journal_restored:
                            context.advance(
                                "step_reused", step_id=step["id"], target_id=step["target_id"]
                            )
                            continue
                        if branch.workspace.can_reuse(step):
                            branch.report["steps"].append({"id": step["id"], "status": "reused"})
                            context.advance(
                                "step_reused", step_id=step["id"], target_id=step["target_id"]
                            )
                            continue
                        check_resources(directory, limits, jobs=step["resources"]["cpu_jobs"])
                        context.event(
                            "step_started", step_id=step["id"], target_id=step["target_id"]
                        )
                        progress.info("Starting " + step["id"])
                        branch.workspace.start(step)
                        branch.execute(step)
                        branch.workspace.complete(step)
                        checkpoints.save(index)
                        context.advance(
                            "step_completed", step_id=step["id"], target_id=step["target_id"]
                        )
                    branch.release()
                    branch.report["status"] = "succeeded"
                    branch.report.pop("error", None)
                    write_json(branch.output / "report.json", branch.report)
                    render_report(branch)
                    if restored != len(branch_steps) - 1:
                        checkpoints.save(len(branch_steps) - 1, complete=True)
                    compact_success(branch)
                    succeeded += 1
                except MakerError as exc:
                    failure = exc
                except Exception as exc:
                    failure = MakerError(f"{type(exc).__name__}: {exc}")
                finally:
                    try:
                        branch.release(
                            keep=bool(
                                failure
                                and not token.requested()
                                and config["defaults"]["execution"]["keep_failed_environment"]
                            )
                        )
                    except MakerError as exc:
                        failure = failure or exc
                    if failure:
                        if hasattr(branch, "workspace"):
                            branch.workspace.fail(branch.step, failure)
                        branch.report.update(status="failed", error=failure.as_dict())
                        write_json(branch.output / "report.json", branch.report)
                        render_report(branch)
                        failures.append({"branch": identifier, "error": failure.as_dict()})
                        error = failure if isinstance(failure, CancelledError) else error or failure
                    reports.append(str(branch.output / "report.json"))
                if isinstance(error, CancelledError) or context.state["resources"]:
                    break
            qualification = "unverified"
            if not failures and request.plan["spec"]["run_db_tests"]:
                if request.plan["spec"]["goal"] in {"verified", "package"}:
                    complete_matrix([Path(p) for p in reports])
                qualification = (
                    "package-verified"
                    if request.plan["spec"]["goal"] in {"verified", "package"}
                    else "source-verified"
                )
            result = {
                "reports": [{"path": p, "sha256": file_hash(Path(p))} for p in reports],
                "qualification": qualification,
                "db_tests": "skipped"
                if not request.plan["spec"]["run_db_tests"]
                else "failed"
                if failures
                else "passed",
                "publication_eligible": qualification == "package-verified",
                "failures": failures,
            }
        except MakerError as exc:
            error = exc
        except Exception as exc:
            error = MakerError(f"{type(exc).__name__}: {exc}")
        status = (
            "succeeded"
            if error is None
            else "cancelled"
            if isinstance(error, CancelledError)
            else "partial"
            if succeeded
            else "failed"
        )
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
            progress.info("Build completed")
    return 5 if status == "partial" else error.exit_code if error else 0
