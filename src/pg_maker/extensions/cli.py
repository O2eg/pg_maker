"""CLI adapter for standalone extension projects."""

from pathlib import Path

from pg_maker.extensions.config import BUILTINS
from pg_maker.storage import write_json


def add_commands(sub):
    extension = sub.add_parser("extension", help="build independent DEB/RPM extension packages")
    commands = extension.add_subparsers(dest="extension_command", required=True)
    init = commands.add_parser("init", help="create an editable extension project")
    init.add_argument("directory", type=Path)
    init.add_argument(
        "--extension", action="append", dest="extensions", choices=BUILTINS, required=True
    )
    init.add_argument("--major", type=int, default=18)
    init.add_argument("--profile")
    init.add_argument("--packages", type=Path)
    init.add_argument(
        "--platform", choices=["debian-13", "rocky-9", "rocky-10"], default="debian-13"
    )
    init.add_argument("--architecture", choices=["amd64", "arm64"], default="amd64")
    init.add_argument(
        "--split-bundled",
        action="store_true",
        help="create a new revision of YC input DEBs without selected bundled modules",
    )
    prepare = commands.add_parser("prepare", help="pin SDK, source inputs and build/test images")
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("--out", dest="output", type=Path, required=True)
    prepare.add_argument("--input-store", type=Path, default=Path("inputs"))
    prepare.add_argument("--offline", action="store_true")
    plan = commands.add_parser("plan", help="inspect the locked extension build steps")
    plan.add_argument("lock", type=Path)
    plan.add_argument("--out", type=Path)
    build = commands.add_parser("build", help="compile, package and test pinned extensions")
    build.add_argument("lock", type=Path)
    build.add_argument("--publish-config", type=Path)
    build.add_argument("--input-store", type=Path)
    for command in (prepare, build):
        command.add_argument("--operations", type=Path, default=Path("operations"))
        command.add_argument("--operation-id")
        command.add_argument("--detach", action="store_true")


def dispatch(service, args, *, request_id=None):
    name = args.extension_command
    options = {
        k: v
        for k, v in vars(args).items()
        if k
        not in {
            "command",
            "extension_command",
            "machine",
            "request_id",
            "component_capabilities",
            "operations",
            "detach",
            "out",
        }
    }
    for key, value in list(options.items()):
        if isinstance(value, Path):
            options[key] = value.expanduser().absolute()
    if name in {"prepare", "build"}:
        options["request_id"] = request_id
    result = getattr(service, "extension_" + name)(**options)
    if name == "plan" and args.out:
        write_json(args.out, result)
    if name in {"prepare", "build"} and not args.detach:
        result = service.wait(service.directory / result["operation_id"])
    return result
