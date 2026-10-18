"""Thin argparse adapter. Machine stdout contains exactly one JSON document."""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from pg_maker import __version__
from pg_maker.api import MakerService
from pg_maker.assets import initialize_project
from pg_maker.errors import MakerError, ValidationError
from pg_maker.integrations.machine import EXIT_CODES, capabilities, envelope
from pg_maker.models import CommandSpec, EnvironmentSpec, OperationRequest
from pg_maker.runners.docker import current_user
from pg_maker.security import redact
from pg_maker.storage import canonical_json, read_json, write_json


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValidationError(message)


def parser() -> Parser:
    root = Parser(prog="pg-maker", description="PostgreSQL distribution configuration and planning")
    root.add_argument("--version", action="version", version=f"pg-maker {__version__}")
    sub = root.add_subparsers(dest="command", required=True)
    from pg_maker.development.cli import add_commands

    add_commands(sub)
    init = sub.add_parser("init", help="create editable YAML configuration and document schemas")
    init.add_argument("directory", type=Path)
    init_docs = init.add_mutually_exclusive_group()
    init_docs.add_argument("--with-doc", dest="with_doc", action="store_true", default=None)
    init_docs.add_argument("--without-doc", dest="with_doc", action="store_false")
    init.add_argument("--doc-language", action="append", dest="doc_languages")
    init.add_argument("--feature", action="append", dest="features")
    init.add_argument("--build-mode", choices=["release", "debug"])
    sub.add_parser("doctor", help="check the current Docker daemon")
    runner = sub.add_parser("runner", help="container executor checks")
    checks = runner.add_subparsers(dest="runner_command", required=True)
    check = checks.add_parser("check", help="run uname/id in an isolated pinned image")
    check.add_argument("--image", required=True, help="local sha256 image ID or repository@sha256")
    check.add_argument("--out", type=Path, default=Path("operations"))
    check.add_argument("--operation-id")
    check.add_argument("--detach", action="store_true")

    def config_args(command):
        command.add_argument("manifest", type=Path)
        command.add_argument("--config-dir", type=Path)
        command.add_argument("--feature", action="append", dest="features")
        command.add_argument("--build-mode", choices=["release", "debug"])
        docs = command.add_mutually_exclusive_group()
        docs.add_argument("--with-doc", dest="with_doc", action="store_true", default=None)
        docs.add_argument("--without-doc", dest="with_doc", action="store_false")
        command.add_argument("--doc-language", action="append", dest="doc_languages")

    config = sub.add_parser("config").add_subparsers(dest="config_command", required=True)
    show = config.add_parser("show")
    config_args(show)
    show.add_argument(
        "--resolved", action="store_true", help="show effective values and provenance"
    )
    config_args(sub.add_parser("validate"))
    config_args(sub.add_parser("features"))
    names = sub.add_parser("package").add_subparsers(dest="package_command", required=True)
    config_args(names.add_parser("names"))
    sub.add_parser("platforms", help="list installed platform recipes")
    env = sub.add_parser("env").add_subparsers(dest="env_command", required=True)
    for command in (env.add_parser("prepare"), sub.add_parser("resolve")):
        config_args(command)
        command.add_argument("--out", required=True, type=Path)
        command.add_argument("--operations", type=Path, default=Path("operations"))
        command.add_argument("--input-store", type=Path, default=Path("inputs"))
        command.add_argument("--operation-id")
        command.add_argument("--detach", action="store_true")
        command.add_argument("--offline", action="store_true")
        if command.prog.endswith("resolve"):
            command.add_argument("--environments", type=Path, required=True)
    plan = sub.add_parser("plan", help="produce a read-only build DAG from fixed inputs")
    plan.add_argument("lock", type=Path)
    plan.add_argument("--out", type=Path)
    plan.add_argument("--input-store", type=Path)
    plan.add_argument(
        "--goal",
        choices=[
            "source",
            "build",
            "package",
            "docs",
            "translations",
            "doc-source",
            "translated-docs",
            "verified",
        ],
        default="build",
    )
    build = sub.add_parser("build", help="execute a locked source or build graph")
    build.add_argument("lock", type=Path)
    build.add_argument(
        "--goal",
        choices=[
            "source",
            "build",
            "package",
            "docs",
            "translations",
            "doc-source",
            "translated-docs",
            "verified",
        ],
        default=None,
    )
    build.add_argument("--plan", type=Path)
    build.add_argument("--plan-hash")
    build.add_argument("--input-store", type=Path)
    build.add_argument("--operations", type=Path, default=Path("operations"))
    build.add_argument("--operation-id")
    build.add_argument("--detach", action="store_true")
    build.add_argument("--cache-store", type=Path)
    build.add_argument("--no-cache", action="store_true")
    build_docs = build.add_mutually_exclusive_group()
    build_docs.add_argument("--with-doc", dest="with_doc", action="store_true", default=None)
    build_docs.add_argument("--without-doc", dest="with_doc", action="store_false")
    build.add_argument("--doc-language", action="append", dest="doc_languages")
    build.add_argument("--feature", action="append", dest="features")
    build.add_argument("--build-mode", choices=["release", "debug"])
    docs = sub.add_parser("docs", help="containerized documentation tools")
    docs_commands = docs.add_subparsers(dest="docs_command", required=True)
    for name, goal in (
        ("build", "docs"),
        ("write", "doc-source"),
        ("prepare-translations", "translations"),
        ("apply-translations", "translated-docs"),
    ):
        command = docs_commands.add_parser(name)
        command.set_defaults(goal=goal, run_db_tests=False, cache_store=None, no_cache=True)
        command.add_argument("lock", type=Path)
        command.add_argument("--plan", type=Path)
        command.add_argument("--plan-hash")
        command.add_argument("--input-store", type=Path)
        command.add_argument("--operations", type=Path, default=Path("operations"))
        command.add_argument("--operation-id")
        command.add_argument("--detach", action="store_true")
    resume = sub.add_parser("resume", help="resume a failed operation with verified checkpoints")
    resume.add_argument("directory", type=Path)
    resume.add_argument("--plan-hash")
    resume.add_argument("--input-hash")
    resume.add_argument("--detach", action="store_true")
    export = sub.add_parser("export", help="copy portable artifacts, reports and logs")
    export.add_argument("directory", type=Path)
    export.add_argument("--out", required=True, type=Path)
    export.add_argument("--include-inputs", action="store_true")
    cleanup = sub.add_parser(
        "cleanup", help="Preview or apply private scratch removal after export"
    )
    cleanup.add_argument("directory", type=Path)
    cleanup.add_argument("--export", dest="exported", type=Path)
    cleanup.add_argument("--apply", type=Path)
    cleanup.add_argument("--out", type=Path)
    changes = sub.add_parser(
        "changes", help="Create private agent workspaces and export ChangeSets"
    )
    changes_commands = changes.add_subparsers(dest="changes_command", required=True)
    change_workspace = changes_commands.add_parser("workspace")
    change_workspace.add_argument("directory", type=Path)
    change_workspace.add_argument("--input-store", type=Path, required=True)
    change_workspace.add_argument("--source-hash", required=True)
    change_workspace.add_argument("--lock", type=Path)
    change_workspace.add_argument("--feature")
    change_workspace.add_argument("--until")
    change_export = changes_commands.add_parser("export")
    change_export.add_argument("directory", type=Path)
    change_export.add_argument("--out", type=Path, required=True)
    change_export.add_argument("--id", required=True)
    change_export.add_argument("--postgresql", required=True)
    changes_commands.add_parser("validate").add_argument("file", type=Path)
    runtime = sub.add_parser("runtime", help="Create a pg_stand image from locked DEBs")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_plan = runtime_commands.add_parser("plan")
    runtime_plan.add_argument("directory", type=Path)
    runtime_plan.add_argument("--profile", type=Path, required=True)
    runtime_plan.add_argument("--out", type=Path)
    runtime_build = runtime_commands.add_parser("build")
    runtime_build.add_argument("plan", type=Path)
    runtime_build.add_argument("--plan-hash", required=True)
    runtime_build.add_argument("--out", type=Path, required=True)
    runtime_build.add_argument("--operations", type=Path, default=Path("operations"))
    runtime_build.add_argument("--operation-id")
    runtime_build.add_argument("--detach", action="store_true")
    cache = sub.add_parser("cache").add_subparsers(dest="cache_command", required=True)
    prune = cache.add_parser("prune", help="preview or apply a checked cache cleanup plan")
    prune.add_argument("directory", type=Path)
    prune.add_argument("--out", type=Path)
    prune.add_argument("--apply", type=Path)
    for command in (plan, build):
        tests = command.add_mutually_exclusive_group()
        tests.add_argument(
            "--run-db-tests",
            dest="run_db_tests",
            action="store_true",
            default=None,
            help="run PostgreSQL, component and acceptance tests (build default: disabled)",
        )
        tests.add_argument(
            "--skip-db-tests",
            dest="run_db_tests",
            action="store_false",
            help="skip DB tests and baseline; produce an unverified development build",
        )
    for name in ("status", "events", "cancel", "wait", "artifacts"):
        command = sub.add_parser(name)
        command.add_argument("directory", type=Path)
        if name == "events":
            command.add_argument("--after-sequence", type=int, default=0)
            command.add_argument("--limit", type=int, default=100)
        if name == "wait":
            command.add_argument("--timeout", type=int)
    # Document global flags; a small pre-parser accepts them before or after the command.
    root.add_argument("--machine", action="store_true", help="one component JSON response")
    root.add_argument("--request-id")
    root.add_argument("--component-capabilities", action="store_true")
    return root


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    machine = "--machine" in values
    request_id, command, result, error, code, status = None, "arguments", None, None, 0, "succeeded"
    try:
        common = Parser(add_help=False, allow_abbrev=False)
        common.add_argument("--machine", action="store_true")
        common.add_argument("--request-id")
        common.add_argument("--component-capabilities", action="store_true")
        flags, rest = common.parse_known_args(values)
        machine, request_id = flags.machine, flags.request_id
        if flags.component_capabilities:
            document = capabilities()
            if machine:
                document = envelope("capabilities", request_id, result=document)
            print(canonical_json(document))
            return 0
        if machine:
            output = io.StringIO()
            try:
                with redirect_stdout(output):
                    args = parser().parse_args(rest)
            except SystemExit as exc:
                if exc.code != 0:
                    raise
                command = "version" if "--version" in rest else "help"
                print(
                    canonical_json(
                        envelope(command, request_id, result={command: output.getvalue()})
                    )
                )
                return 0
        else:
            args = parser().parse_args(rest)
        subcommand = {
            "runner": "check",
            "config": "show",
            "package": "names",
            "env": "prepare",
            "cache": "prune",
        }
        command = args.command + (
            " " + subcommand[args.command] if args.command in subcommand else ""
        )
        if args.command == "docs":
            command += " " + args.docs_command
        if args.command == "runtime":
            command += " " + args.runtime_command
        if args.command == "changes":
            command += " " + args.changes_command
        if args.command == "dev":
            command += " " + args.dev_command
        if args.command == "init":
            result = initialize_project(
                args.directory.expanduser().absolute(),
                with_doc=args.with_doc,
                doc_languages=args.doc_languages,
                features=args.features,
                build_mode=args.build_mode,
            )
        else:
            out = (
                args.out
                if args.command == "runner"
                else getattr(args, "operations", Path("operations"))
            )
            service = MakerService(out.expanduser().absolute())
            options = {
                key: getattr(args, key)
                for key in ("config_dir", "features", "build_mode", "with_doc", "doc_languages")
                if hasattr(args, key)
            }
            if args.command == "dev":
                from pg_maker.development.cli import dispatch

                result = dispatch(service, args)
            elif args.command == "changes":
                from pg_maker.changes.workspace import export_changes, validate_changes, workspace

                if args.changes_command == "workspace":
                    result = workspace(
                        args.input_store.absolute(),
                        args.source_hash,
                        args.directory.absolute(),
                        lock=args.lock,
                        feature=args.feature,
                        until=args.until,
                    )
                elif args.changes_command == "export":
                    result = export_changes(
                        args.directory.absolute(), args.out.absolute(), args.id, args.postgresql
                    )
                else:
                    result = validate_changes(args.file.absolute())
            elif args.command == "runtime":
                from pg_maker.integrations.runtime import plan_runtime, verify_runtime_plan
                from pg_maker.models import RuntimeRequest

                if args.runtime_command == "plan":
                    result = plan_runtime(args.directory, read_json(args.profile))
                    if args.out:
                        write_json(args.out.absolute(), result)
                else:
                    document = read_json(args.plan)
                    verify_runtime_plan(document, args.plan_hash)
                    result = service.start_operation(
                        RuntimeRequest(document, str(args.out.absolute())),
                        operation_id=args.operation_id,
                        request_id=request_id,
                    )
                    if not args.detach:
                        result = service.wait(service.directory / result["operation_id"])
                    status = result["status"]
                    if status in {"failed", "cancelled", "partial", "interrupted"}:
                        error = result.get("error")
                        code = EXIT_CODES.get((error or {}).get("code"), 6)
            elif args.command in {"config", "features", "package", "validate", "env", "resolve"}:
                if args.command == "validate":
                    result = service.validate(args.manifest, **options)
                elif args.command in {"env", "resolve"}:
                    prepare = (
                        service.resolve
                        if args.command == "resolve"
                        else service.prepare_environments
                    )
                    env_option = (
                        {"environments": args.environments} if args.command == "resolve" else {}
                    )
                    result = prepare(
                        args.manifest,
                        output=args.out,
                        input_store=args.input_store,
                        offline=args.offline,
                        request_id=request_id,
                        operation_id=args.operation_id,
                        **env_option,
                        **options,
                    )
                    directory = service.directory / result["operation_id"]
                    if not args.detach:
                        if not machine:
                            print(
                                f"Operation {result['operation_id']}; logs: {directory}",
                                file=sys.stderr,
                            )
                        result = service.wait(directory)
                else:
                    result = service.config_show(args.manifest, **options)
                    if args.command == "features":
                        result = {"features": result["data"]["features"]}
                    elif args.command == "package":
                        result = {
                            "variants": [
                                {"id": v["id"], **v["identity"]} for v in result["data"]["variants"]
                            ]
                        }
            elif args.command == "platforms":
                from pg_maker.config import resource_path
                from pg_maker.yamlio import load_yaml

                result = {
                    "platforms": [
                        load_yaml(path, "platform")
                        for path in sorted(resource_path("recipes", "platforms").glob("*.yaml"))
                    ]
                }
            elif args.command == "plan":
                result = service.plan(
                    args.lock,
                    goal=args.goal,
                    input_store=args.input_store,
                    run_db_tests=args.run_db_tests,
                )
                if args.out:
                    write_json(args.out.absolute(), result)
            elif args.command in {"build", "docs"}:
                result = service.build(
                    args.lock,
                    goal=args.goal,
                    plan=args.plan,
                    plan_hash=args.plan_hash,
                    run_db_tests=args.run_db_tests,
                    input_store=args.input_store,
                    cache_store=args.cache_store,
                    use_cache=not args.no_cache,
                    operation_id=args.operation_id,
                    request_id=request_id,
                    **{
                        key: getattr(args, key)
                        for key in ("with_doc", "doc_languages", "features", "build_mode")
                        if hasattr(args, key)
                    },
                )
                directory = service.directory / result["operation_id"]
                if not args.detach:
                    if not machine:
                        print(
                            f"Operation {result['operation_id']}; logs: {directory}",
                            file=sys.stderr,
                        )
                    result = service.wait(directory)
            elif args.command == "resume":
                directory = args.directory.expanduser().absolute()
                result = service.resume(
                    directory, plan_hash=args.plan_hash, input_hash=args.input_hash
                )
                if not args.detach:
                    result = service.wait(directory)
            elif args.command == "export":
                result = service.export(
                    args.directory.expanduser().absolute(),
                    args.out,
                    include_inputs=args.include_inputs,
                )
            elif args.command == "cleanup":
                from pg_maker.execution.cleanup import apply_cleanup, cleanup_plan

                if args.apply:
                    document = read_json(args.apply)
                    if document.get("operation") != str(args.directory.absolute()):
                        raise ValidationError("cleanup plan belongs to another operation")
                    result = apply_cleanup(service, document)
                elif args.exported:
                    result = cleanup_plan(
                        service, args.directory.absolute(), args.exported.absolute()
                    )
                else:
                    raise ValidationError("cleanup requires --export for preview or --apply")
                if args.out:
                    write_json(args.out.absolute(), result)
            elif args.command == "cache":
                from pg_maker.execution.cache import SnapshotStore

                cache_store = SnapshotStore(args.directory.expanduser().absolute())
                result = (
                    cache_store.prune(read_json(args.apply))
                    if args.apply
                    else cache_store.prune_plan()
                )
                if args.out:
                    write_json(args.out.absolute(), result)
            elif args.command == "doctor":
                result = service.doctor()
            elif args.command == "runner":
                request = OperationRequest(
                    EnvironmentSpec(args.image, current_user()),
                    (
                        CommandSpec("kernel", ("uname", "-s"), timeout_seconds=30),
                        CommandSpec("user", ("id", "-u"), timeout_seconds=30),
                    ),
                )
                result = service.start_operation(
                    request, operation_id=args.operation_id, request_id=request_id
                )
                directory = service.directory / result["operation_id"]
                if not args.detach:
                    if not machine:
                        print(
                            f"Operation {result['operation_id']}; logs: {directory}",
                            file=sys.stderr,
                        )
                    result = service.wait(directory)
            else:
                directory = args.directory.expanduser().absolute()
                if args.command == "artifacts":
                    from pg_maker.artifacts.catalog import operation_artifacts

                    result = operation_artifacts(directory, service.status(directory))
                elif args.command == "events":
                    result = service.events(
                        directory, after_sequence=args.after_sequence, limit=args.limit
                    )
                elif args.command == "wait":
                    result = service.wait(directory, timeout_seconds=args.timeout)
                else:
                    result = getattr(service, args.command)(directory)
            if args.command == "dev" and args.dev_command in {"build", "exec"}:
                status = result["status"]
                if not args.detach:
                    error = result.get("error")
                    code = (
                        0 if status == "succeeded" else EXIT_CODES.get((error or {}).get("code"), 6)
                    )
            elif args.command == "wait" or (
                args.command in {"runner", "env", "resolve", "build", "docs", "resume"}
                and not args.detach
            ):
                status = result["status"]
                error = result["error"]
                code = (
                    0
                    if status == "succeeded"
                    else 5
                    if status == "partial"
                    else EXIT_CODES.get((error or {}).get("code"), 6)
                )
            elif args.command in {"runner", "env", "resolve", "build", "docs", "resume"}:
                status = result["status"]
    except MakerError as exc:
        code, error, status = exc.exit_code, exc.as_dict(), "failed"
    except KeyboardInterrupt:
        code, error, status = (
            130,
            {"code": "wait_interrupted", "message": "worker was not cancelled"},
            "failed",
        )
    except (OSError, ValueError) as exc:
        code, error, status = 6, MakerError(f"{type(exc).__name__}: {exc}").as_dict(), "failed"
    if machine:
        print(
            canonical_json(envelope(command, request_id, result=result, error=error, status=status))
        )
    elif error:
        print(f"{error['code']}: {error['message']}", file=sys.stderr)
    else:
        print(json.dumps(redact(result), ensure_ascii=False, indent=2))
    return code
