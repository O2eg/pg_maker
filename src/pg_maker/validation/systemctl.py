#!/usr/bin/python3
"""Strict test-only systemctl subset for pg_maker's generated units.

This is an independent, small harness, not the legacy EUPL systemctl implementation.
It never claims native systemd/cgroup behavior and rejects unsupported directives.
"""

from __future__ import annotations

import configparser
import os
import pwd
import shlex
import subprocess
import sys
import time
from pathlib import Path

SUPPORTED = {
    "Unit": {"Description", "After"},
    "Service": {
        "Type",
        "User",
        "Group",
        "WorkingDirectory",
        "Environment",
        "PIDFile",
        "ExecStartPre",
        "ExecStart",
        "ExecStop",
        "ExecReload",
        "TimeoutStartSec",
        "TimeoutStopSec",
    },
    "Install": {"WantedBy"},
}


def load_unit(name):
    if "/" in name or not name.endswith(".service"):
        raise ValueError("an explicit service name is required")
    path = Path("/usr/lib/systemd/system") / name
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with path.open() as stream:
        parser.read_file(stream)
    for section in parser.sections():
        if section not in SUPPORTED or set(parser[section]) - SUPPORTED[section]:
            raise ValueError("unsupported unit directive")
    service = dict(parser["Service"])
    if service["Type"] != "forking":
        raise ValueError("only forking units are supported")
    if parser["Install"]["WantedBy"] != "multi-user.target":
        raise ValueError("unsupported install target")
    if any("%" in value for section in parser.sections() for value in parser[section].values()):
        raise ValueError("unit specifiers are unsupported by this harness")
    return service


def pid(service):
    try:
        lines = Path(service["PIDFile"]).read_text().splitlines()
        number = int(lines[0])
        data = str(Path(service["PIDFile"]).parent)
        if len(lines) < 2 or lines[1] != data:
            return None
        if number <= 1:
            return None
        process = Path("/proc") / str(number)
        if process.stat().st_uid != pwd.getpwnam(service["User"]).pw_uid:
            return None
        if process.joinpath("stat").read_text().rsplit(")", 1)[1].split()[0] == "Z":
            return None
        command = process.joinpath("cmdline").read_bytes().split(b"\0")[0].decode()
        expected = str(Path(shlex.split(service["ExecStart"])[0]).with_name("postgres"))
        if command != expected and not command.startswith(expected + " "):
            return None
        arguments = shlex.split(
            process.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode()
        )
        if "-D" not in arguments or arguments[arguments.index("-D") + 1] != data:
            return None
        os.kill(number, 0)
        return number
    except (OSError, ValueError, KeyError, IndexError):
        return None


def execute(service, key):
    command = service[key]
    privileged = command.startswith("+")
    argv = shlex.split(command[1:] if privileged else command)
    if not argv or not argv[0].startswith("/"):
        raise ValueError("unit executable must be absolute")
    environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8"}
    for item in shlex.split(service.get("Environment", "")):
        name, value = item.split("=", 1)
        environment[name] = value
    options = (
        {}
        if privileged
        else {
            "user": service["User"],
            "group": service["Group"],
            "extra_groups": [],
        }
    )
    result = subprocess.run(
        argv,
        cwd="/" if privileged else service["WorkingDirectory"],
        env=environment,
        timeout=int(service["TimeoutStopSec" if key == "ExecStop" else "TimeoutStartSec"]),
        check=False,
        **options,
    )
    if result.returncode:
        raise RuntimeError(key + " failed with status " + str(result.returncode))


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    quiet = "--quiet" in arguments
    arguments = [a for a in arguments if a != "--quiet"]
    if arguments == ["--version"]:
        print("pg_maker systemctl test harness v1; service_manager=emulated")
        return 0
    if arguments == ["daemon-reload"]:
        return 0
    if len(arguments) != 2:
        raise ValueError("unsupported systemctl invocation")
    command, name = arguments
    if command not in {
        "start",
        "stop",
        "restart",
        "reload",
        "status",
        "is-active",
        "enable",
        "disable",
    }:
        raise ValueError("unsupported systemctl command")
    service = load_unit(name)
    if command in {"enable", "disable"}:
        link = Path("/etc/systemd/system/multi-user.target.wants") / name
        if command == "enable":
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.is_symlink():
                if os.readlink(link) != "/usr/lib/systemd/system/" + name:
                    raise ValueError("conflicting service link")
            else:
                link.symlink_to("/usr/lib/systemd/system/" + name)
        else:
            link.unlink(missing_ok=True)
        return 0
    if command in {"status", "is-active"}:
        active = pid(service) is not None
        if not quiet:
            print("active" if active else "inactive")
        return 0 if active else 3
    if command in {"stop", "restart"} and pid(service):
        execute(service, "ExecStop")
        deadline = time.monotonic() + int(service["TimeoutStopSec"])
        while pid(service) and time.monotonic() < deadline:
            time.sleep(0.05)
        if pid(service):
            raise RuntimeError("service did not stop")
    if command in {"start", "restart"} and not pid(service):
        execute(service, "ExecStartPre")
        execute(service, "ExecStart")
        if not pid(service):
            raise RuntimeError("service command returned success without a running server")
    if command == "reload":
        if not pid(service):
            raise RuntimeError("cannot reload an inactive service")
        execute(service, "ExecReload")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("pg-maker systemctl: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
