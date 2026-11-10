"""CLI adapter for explicit and post-build publication."""

from pathlib import Path

from pg_maker.storage import write_json


def add_commands(sub):
    command = sub.add_parser("publish", help="publish tested packages to configured receivers")
    actions = command.add_subparsers(dest="publish_command", required=True)
    for name in ("plan", "run"):
        parser = actions.add_parser(name)
        parser.add_argument(
            "source", type=Path, help="successful operation or portable export directory"
        )
        parser.add_argument("--config", type=Path, required=True)
        if name == "plan":
            parser.add_argument("--out", type=Path)
        else:
            parser.add_argument("--wait-for-source", action="store_true")
            parser.add_argument("--operations", type=Path, default=Path("operations"))
            parser.add_argument("--operation-id")
            parser.add_argument("--detach", action="store_true")


def dispatch(service, args, request_id):
    if args.publish_command == "plan":
        result = service.publication_plan(args.source.absolute(), args.config.absolute())
        if args.out:
            write_json(args.out.absolute(), result)
        return result
    result = service.publish(
        args.source.absolute(),
        args.config.absolute(),
        wait_for_source=args.wait_for_source,
        operation_id=args.operation_id,
        request_id=request_id,
    )
    return result if args.detach else service.wait(service.directory / result["operation_id"])
