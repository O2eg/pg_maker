"""Run the pinned pg_textsearch shell-suite inventory after SQL regression."""

import json
import os
import subprocess
from pathlib import Path


def main():
    root = Path.cwd()
    scripts = root / "test/scripts"
    # This file defines functions sourced by the replication scripts.
    libraries = {"replication_lib.sh"}
    selected = sorted(p for p in scripts.glob("*.sh") if p.name not in libraries)
    if not selected:
        raise RuntimeError("empty pg_textsearch shell suite")
    outcomes = []
    env = {
        k: v for k, v in os.environ.items() if k not in {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER"}
    }
    for path in selected:
        log = root / ("pg-maker-" + path.stem + ".log")
        with log.open("w") as output:
            result = subprocess.run(
                ["bash", path.name], cwd=scripts, env=env, stdout=output, stderr=subprocess.STDOUT
            )
        print(log.read_text(errors="replace"), end="", flush=True)
        outcomes.append({"script": path.name, "returncode": result.returncode})
        (root / "pg-maker-shell-tests.log").write_text(json.dumps(outcomes, indent=2))
        result.check_returncode()
    print("PG_MAKER_TEXTSEARCH_SUITES_PASSED=" + str(len(outcomes)), flush=True)


if __name__ == "__main__":
    main()
