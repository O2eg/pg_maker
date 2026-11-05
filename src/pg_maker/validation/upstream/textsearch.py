"""Run the pinned pg_textsearch shell-suite inventory after SQL regression."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def run_suites(root, scripts):
    # This file defines functions sourced by the replication scripts.
    libraries = {"replication_lib.sh"}
    selected = sorted(p for p in scripts.glob("*.sh") if p.name not in libraries)
    if not selected:
        raise RuntimeError("empty pg_textsearch shell suite")
    outcomes = []
    env = {
        k: v for k, v in os.environ.items() if k not in {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER"}
    }
    # Fixtures without an explicit socket path use the writable test directory.
    env["PGHOST"] = "/tmp"
    for path in selected:
        # This scenario uses the parent test cluster; the others create their own.
        test_env = dict(os.environ) if path.name == "hypertable.sh" else dict(env)
        if path.name == "replication_compat.sh":
            test_env["PG_MAKER_TEST_PRELOAD"] = "pg_textsearch,timescaledb"
        log = root / ("pg-maker-" + path.stem + ".log")
        with log.open("w") as output:
            result = subprocess.run(
                ["bash", path.name],
                cwd=scripts,
                env=test_env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        text = log.read_text(errors="replace")
        print(text, end="", flush=True)
        outcomes.append({"script": path.name, "returncode": result.returncode})
        (root / "pg-maker-shell-tests.log").write_text(json.dumps(outcomes, indent=2))
        result.check_returncode()
        required = {
            "hypertable.sh": "All hypertable tests passed!",
            "replication_compat.sh": "F2 PASSED",
        }.get(path.name)
        if required and required not in text:
            raise RuntimeError(path.name + " did not execute its TimescaleDB assertions")
    print("PG_MAKER_TEXTSEARCH_SUITES_PASSED=" + str(len(outcomes)), flush=True)


def main():
    root = Path.cwd()
    # Upstream places Unix sockets beside its scripts. Deep operation paths
    # exceed Linux sockaddr_un limits, so run the unchanged suite in /tmp.
    with tempfile.TemporaryDirectory(prefix="pts-", dir="/tmp") as short:
        test = Path(short) / "test"
        shutil.copytree(root / "test", test)
        try:
            run_suites(root, test / "scripts")
        finally:
            for log in test.rglob("*.log"):
                target = root / "pg-maker-shell-artifacts" / log.relative_to(test)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(log, target)


if __name__ == "__main__":
    main()
