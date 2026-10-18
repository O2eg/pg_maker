"""Run the same public CLI used locally; database tests require an explicit flag."""

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import yaml

from pg_maker.api import MakerService
from pg_maker.config import merge
from pg_maker.models import TERMINAL_STATUSES
from pg_maker.runners.docker import current_user
from pg_maker.storage import read_json


def cli(*arguments):
    result = subprocess.run(
        [sys.executable, "-m", "pg_maker", *map(str, arguments), "--machine"],
        text=True,
        stdout=subprocess.PIPE,
        check=False,
    )
    response = json.loads(result.stdout)
    if result.returncode:
        raise RuntimeError(response)
    return response["result"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--fixtures-only", action="store_true")
    parser.add_argument("--run-db-tests", action="store_true")
    parser.add_argument("--with-doc", action="store_true")
    args = parser.parse_args()
    root = args.work.absolute()
    cli("init", root)
    if args.fixtures_only or args.with_doc:
        profile_path = root / "config/documentation.yaml"
        profile = yaml.safe_load(profile_path.read_text())
        profile["spec"].update(enabled=True, formats=["html", "man", "pdf"])
        if args.fixtures_only:
            profile["spec"].update(languages=["en", "ru"], html_backend="sphinx")
        profile_path.write_text(yaml.safe_dump(profile))
    defaults_path = root / "config/defaults.yaml"
    defaults = yaml.safe_load(defaults_path.read_text())
    cpus = min(4, len(os.sched_getaffinity(0)))
    defaults = merge(
        defaults,
        {
            "spec": {
                "build": {"jobs": cpus},
                "execution": {
                    "build_user": current_user(),
                    "limits": {"min_free_disk_bytes": 2 * 1024**3, "memory_bytes": 4 * 1024**3},
                },
                "validation": {"run_db_tests": args.run_db_tests},
            }
        },
    )
    defaults_path.write_text(yaml.safe_dump(defaults))
    service = MakerService(root / "operations")

    def operation(*arguments):
        started = cli(*arguments, "--operations", service.directory, "--detach")
        state = service.wait(service.directory / started["operation_id"])
        if state["status"] != "succeeded":
            raise RuntimeError(state)
        return state

    def interrupted(_number, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        operation(
            "env",
            "prepare",
            root / "build.yaml",
            "--out",
            root / "environments.json",
            "--input-store",
            root / "inputs",
            "--operation-id",
            "environments",
        )
        if args.fixtures_only:
            images = {
                i["role"]: i["image_id"]
                for i in read_json(root / "environments.json")["spec"]["images"]
            }
            env = {k: v for k, v in os.environ.items() if k != "PG_MAKER_BUILD_LOCK"}
            env.update(
                PG_MAKER_BUILD_IMAGE=images["build"],
                PG_MAKER_DOCKER_IMAGE=images["test"],
                PG_MAKER_PREPARATION_IMAGE=images["build"],
                PG_MAKER_DEVELOPMENT_IMAGE=images["build"],
                PG_MAKER_PACKAGE_ENVIRONMENTS=str(root / "environments.json"),
                PG_MAKER_DOC_ENVIRONMENTS=str(root / "environments.json"),
            )
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/integration",
                    "--basetemp=" + str(root / "pytest"),
                ],
                env=env,
                check=True,
            )
        else:
            operation(
                "resolve",
                root / "build.yaml",
                "--environments",
                root / "environments.json",
                "--out",
                root / "lock.json",
                "--input-store",
                root / "inputs",
                "--operation-id",
                "resolve",
            )
            goal = "verified" if args.run_db_tests else "package"
            plan = cli("plan", root / "lock.json", "--goal", goal, "--out", root / "plan.json")
            operation(
                "build",
                root / "lock.json",
                "--goal",
                goal,
                "--plan",
                root / "plan.json",
                "--plan-hash",
                plan["plan_hash"],
                "--operation-id",
                "build",
            )
    finally:
        original_error = sys.exc_info()[1]
        cleanup_errors = []
        directories = list(service.directory.glob("*/input.json"))
        directories += list((root / "pytest").glob("**/operations/*/input.json"))
        for path in directories:
            try:
                state = service.status(path.parent)
                if state["status"] not in TERMINAL_STATUSES or state["resources"]:
                    service.cancel(path.parent)
                    service.wait(path.parent, timeout_seconds=30)
                relative = path.parent.relative_to(root)
                service.export(path.parent, root / "exports" / relative)
            except Exception as exc:
                print("CI operation cleanup/export: " + str(exc), file=sys.stderr)
                cleanup_errors.append(str(exc))
        if cleanup_errors and original_error is None:
            raise RuntimeError("CI cleanup/export failed: " + "; ".join(cleanup_errors))


if __name__ == "__main__":
    main()
