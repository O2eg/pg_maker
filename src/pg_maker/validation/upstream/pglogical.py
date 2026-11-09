"""Adjust pglogical's test-only config for the PG15 maintenance GUC boundary."""

import re
import subprocess
import sys
from pathlib import Path


def configure_tests(root: Path, version: str) -> None:
    match = re.search(r"PostgreSQL (\d+)\.(\d+)", version)
    if not match or int(match[1]) != 15:
        raise ValueError("the PG15 pglogical test helper requires PostgreSQL 15")
    if int(match[2]) >= 19:
        return
    for name in ("regress-postgresql.conf", "t/perl-95-postgresql.conf"):
        path = root / name
        lines = path.read_text().splitlines(keepends=True)
        path.write_text(
            "".join(line for line in lines if not line.startswith("output_plugin_libraries"))
        )


if __name__ == "__main__":
    output = subprocess.check_output([sys.argv[1] + "/bin/pg_config", "--version"], text=True)
    configure_tests(Path.cwd(), output)
