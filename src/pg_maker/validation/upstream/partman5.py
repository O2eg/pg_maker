"""Exercise pg_partman 5.x's root, role, procedure, BGW and manual fixtures."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from partman import clock_fixture


def procedure_diagnostics(output):
    """Read executed pgTAP diagnostics, excluding commented-out SQL instructions."""
    return re.findall(r'^#.*?"(CALL [^\n]+;)"', output, re.M)


def main():
    root = Path("test")
    expected = {p.relative_to(root).as_posix() for p in root.rglob("*.sql")}
    excluded = {
        p: "upstream marks this scenario not_working_yet"
        for p in expected
        if p.startswith("not_working_yet/")
    }
    completed, counts = set(), []

    def command(argv):
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        result.check_returncode()
        return result.stdout

    def sql(statement):
        return command(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-Atqc", statement])

    def prove(path, user=None):
        name = path.relative_to(root).as_posix()
        if name in completed or name in excluded:
            raise RuntimeError("duplicate or excluded scenario: " + name)
        if "30sec" in path.name:
            time.sleep(30 - time.time() % 30 + 0.1)
        text = command(["pg_prove", "-ovf", *(["-U", user] if user else []), str(path)])
        match = re.findall(r"Files=1, Tests=(\d+)", text)
        if len(match) != 1 or int(match[0]) == 0 or "Result: PASS" not in text:
            raise RuntimeError("missing nonempty passing TAP result: " + name)
        counts.append(int(match[0]))
        completed.add(name)
        return text

    for directory in (root, root / "test_no_search_path", root / "test_pg17plus"):
        for path in sorted(directory.glob("*.sql")):
            prove(path)

    tablespace = (Path.cwd() / "pg-maker-tablespace").resolve()
    tablespace.mkdir()
    sql("CREATE TABLESPACE mytablespace LOCATION '" + str(tablespace).replace("'", "''") + "'")
    for path in sorted((root / "test_tablespace").glob("*.sql")):
        prove(path)
    sql("DROP TABLESPACE mytablespace")

    non_super = root / "test_nonsuperuser"
    prove(non_super / "test-nonsuperuser-part1.sql")
    prove(non_super / "test-id-nonsuperuser-part2.sql", "partman_owner")
    prove(non_super / "test-time-hourly-nonsuperuser-part2.sql", "partman_basic")
    prove(non_super / "test-nonsuperuser-part3.sql")

    for path in sorted((root / "test_procedure").glob("*.sql")):
        for statement in procedure_diagnostics(prove(path)):
            sql(statement)

    sql("ALTER SYSTEM SET pg_partman_bgw.dbname = 'postgres'")
    sql("SELECT pg_reload_conf()")
    for path in sorted((root / "test_bgw").glob("*.sql")):
        prove(path)
    sql("ALTER SYSTEM RESET pg_partman_bgw.dbname")
    sql("SELECT pg_reload_conf()")

    prove(root / "test_manual/test-time-30sec.sql")
    command(["createdb", "partman_public"])
    os.environ["PGDATABASE"] = "partman_public"
    sql("CREATE EXTENSION pg_partman SCHEMA public; CREATE EXTENSION pgtap")
    for path in sorted((root / "test_manual").glob("*public*.sql")):
        prove(path)
    for path in sorted((root / "test_manual/procedures").glob("*.sql")):
        for statement in procedure_diagnostics(prove(path)):
            sql(statement)

    evidence = {
        "expected": sorted(expected),
        "completed": sorted(completed),
        "not_applicable": excluded,
        "assertions": sum(counts),
    }
    Path("pg-maker-tests.log").write_text(json.dumps(evidence, indent=2) + "\n")
    missing = expected - excluded.keys() - completed
    if missing:
        raise RuntimeError("unexecuted upstream scenarios: " + str(sorted(missing)))
    print("PG_MAKER_PARTMAN_TESTS_PASSED=" + str(sum(counts)), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 2:
        clock_fixture(sys.argv[1], __file__)
    else:
        main()
