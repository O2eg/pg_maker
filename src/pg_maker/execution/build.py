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
        self.step = step

    def begin(self):
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

    def command(self, argv, *, cwd=None, env=None, timeout=None, accepted_exit_codes=(0,)):
        self.context.check()
        self.counter += 1
        command = CommandSpec(
            self.id + "." + str(self.counter),
            tuple(argv),
            cwd=cwd or self.container_root + "/obj",
            env={**self.step["env"], **(env or {})},
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
            from pg_maker.sources.store import materialize

            source = self.inputs.tree(self.config["source"]["source_hash"])
            if not seed_core(self, source):
                materialize(source, self.source)
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
            self.inventory = autotools.expected_inventory(self.source, self.obj, self.query)
            self.inventory["extra_tests"] = autotools.extra_tests(
                self.variant["build"], self.config["postgresql_recipe"]
            )
            write_json(self.output / "expected-tests.json", self.inventory)
        elif kind in {"build_and_test_tools", "build_tools"}:
            from pg_maker.execution.component_cache import execute_component

            execute_component(
                self, step["parameters"]["components"][0], run_tests=kind == "build_and_test_tools"
            )
        elif kind == "acceptance_build_prefix":
            components.acceptance(self, self.feature["spec"]["changes"])
        elif kind in {"export_build", "verify_source"}:
            if self.request.plan["spec"]["run_db_tests"]:
                self.report["verified_test_ids"] = autotools.verify_required_tests(
                    self.feature,
                    self.report,
                    read_json(
                        self.output
                        / (
                            "world/world-result.json"
                            if self.branch == "candidate"
                            else "core/core-result.json"
                        )
                    ),
                    self.branch,
                )
                self.report["source_test_scope"] = (
                    "full" if self.branch == "candidate" else "baseline-core"
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
                result = self.command(argv, env=env, timeout=timeout)
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
                    write_json(self.output / "core-files.json", inventory(self.root / "prefix"))
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
                    checkpoints = Checkpoints(branch, branch_steps)
                    restored = checkpoints.restore()
                    branch.begin()
                    for index, step in enumerate(branch_steps):
                        context.check()
                        if index <= restored:
                            context.advance(
                                "step_reused", step_id=step["id"], target_id=step["target_id"]
                            )
                            continue
                        check_resources(directory, limits, jobs=step["resources"]["cpu_jobs"])
                        context.event(
                            "step_started", step_id=step["id"], target_id=step["target_id"]
                        )
                        progress.info("Starting " + step["id"])
                        branch.execute(step)
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
                if request.plan["spec"]["goal"] == "verified":
                    complete_matrix([Path(p) for p in reports])
                qualification = (
                    "package-verified"
                    if request.plan["spec"]["goal"] == "verified"
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
