"""Argument definitions shared by the maker CLI's development adapter."""

from pathlib import Path

from pg_maker.storage import read_json, write_json


def add_commands(sub):
    dev = sub.add_parser("dev", help="owned Git trees, patch stacks and incremental builds")
    commands = dev.add_subparsers(dest="dev_command", required=True)
    command = commands.add_parser("open")
    command.add_argument("directory", type=Path)
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--lock", type=Path)
    source.add_argument("--from-run", type=Path)
    command.add_argument("--feature")
    command.add_argument("--input-store", type=Path)
    command.add_argument("--repository", type=Path)
    commands.add_parser("status").add_argument("directory", type=Path)
    command = commands.add_parser("patch")
    command.add_argument("directory", type=Path)
    command.add_argument(
        "action",
        choices=[
            "import",
            "commit",
            "refresh",
            "pop",
            "push",
            "rebase",
            "continue",
            "abort",
            "export",
            "adopt",
        ],
    )
    command.add_argument("--scope", default="postgresql")
    command.add_argument("--id", dest="identifier")
    command.add_argument("--patch", type=Path)
    command.add_argument("--count", type=int, default=1)
    command.add_argument("--message")
    command.add_argument("--onto-source")
    command.add_argument("--onto-lock", type=Path)
    command.add_argument("--out", dest="output", type=Path)
    for name in ("build", "explain"):
        command = commands.add_parser(name)
        command.add_argument("directory", type=Path)
        command.add_argument(
            "--goal", choices=["source", "build", "package", "docs"], default="build"
        )
        command.add_argument("--component", action="append", dest="components")
        if name == "build":
            command.add_argument("--detach", action="store_true")
    command = commands.add_parser("exec")
    command.add_argument("directory", type=Path)
    command.add_argument("--iteration")
    command.add_argument("--component")
    command.add_argument("--target")
    command.add_argument("--timeout-seconds", type=int, default=3600)
    command.add_argument("--detach", action="store_true")
    command.add_argument("argv", nargs="+")
    command = commands.add_parser("pin")
    command.add_argument("directory", type=Path)
    command.add_argument("iteration")
    command.add_argument("--unpin", action="store_true")
    command = commands.add_parser("prune")
    command.add_argument("directory", type=Path)
    command.add_argument("--keep-last", type=int, default=2)
    command.add_argument("--out", type=Path)
    command.add_argument("--apply", type=Path)


def dispatch(service, args):
    name = args.dev_command
    options = {
        key: value
        for key, value in vars(args).items()
        if key
        not in {
            "command",
            "dev_command",
            "machine",
            "request_id",
            "component_capabilities",
            "detach",
        }
    }
    options["directory"] = options["directory"].absolute()
    if name == "pin":
        options["pinned"] = not options.pop("unpin")
    if name == "prune":
        output = options.pop("out")
        if options["apply"]:
            options["apply"] = read_json(options["apply"])
    if name == "exec":
        name = "execute"
        if options["argv"][:1] == ["--"]:
            options["argv"] = options["argv"][1:]
    result = getattr(service, "dev_" + name)(**options)
    if args.dev_command == "prune" and output:
        write_json(output, result)
    if args.dev_command in {"build", "exec"} and not args.detach:
        result = service.wait(Path(result["operation_directory"]))
    return result
