"""Run pg_partman 4.7.4 suites in the order documented by their SQL scripts.

This helper runs only inside the disposable build container against its test cluster.
It leaves failed suites and their evidence intact and never retries failed assertions.
"""

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def inventory(root):
    files = {p.relative_to(root).as_posix() for p in root.rglob("*.sql")}
    excluded = {
        p: "upstream marks this scenario not_working_yet" for p in files if "/not_working_yet/" in p
    }
    excluded["test_native/test_tablespace/test-time-daily-native-tablespace-template.sql"] = (
        "upstream scenario for PostgreSQL 11 and older; the pg12+ scenario is run"
    )
    excluded.update(
        {
            p: "upstream reapply_indexes.py explicitly rejects native partition sets on PG11+"
            for p in files
            if p.startswith("test_reindex_native/")
        }
    )
    return files, excluded


def procedure_calls(source):
    return [
        call.replace("''", "'")
        for call in re.findall(r'^SELECT diag\(.*?"(CALL [^\n]+;)"', source, re.M)
    ]


def clock_fixture(library):
    """Run calendar-sensitive upstream fixtures on a mid-month Friday.

    Keep time advancing and preserve monotonic clocks. The offset is shared by
    every server/client process; neither host time nor installed DB code changes.
    """
    offset = (dt.date(2026, 5, 15) - dt.datetime.now(dt.timezone.utc).date()).days
    data = Path("pg-maker-clock-data").resolve()
    env = {
        **os.environ,
        "LD_PRELOAD": str(Path(library).resolve()),
        "FAKETIME": str(offset) + "d",
        "FAKETIME_DONT_FAKE_MONOTONIC": "1",
        "PGHOST": "/tmp",
        "PGPORT": "55432",
        "PGUSER": "builder",
        "PGDATABASE": "postgres",
    }

    def run(argv):
        subprocess.run(argv, env=env, check=True)

    run(["initdb", "-D", str(data), "-A", "trust", "--no-locale", "-E", "UTF8", "-U", "builder"])
    with (data / "postgresql.conf").open("a") as stream:
        stream.write(
            "\nlisten_addresses = ''\nport = 55432\nunix_socket_directories = '/tmp'\n"
            "max_locks_per_transaction = 1024\nshared_preload_libraries = 'pg_partman_bgw'\n"
            "pg_partman_bgw.interval = 5\npg_partman_bgw.role = 'builder'\n"
        )
    run(["pg_ctl", "-D", str(data), "-l", str(data / "server.log"), "-w", "start"])
    try:
        run(
            [
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE SCHEMA partman; CREATE EXTENSION pg_partman SCHEMA partman; "
                "CREATE EXTENSION pgtap; SELECT now() AS fixture_clock;",
            ]
        )
        run([sys.executable, __file__])
    finally:
        run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"])


def main():
    root = Path("test")
    expected, excluded = inventory(root)
    completed = set()
    counts = []

    def command(argv):
        result = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="", flush=True)
        result.check_returncode()
        return result.stdout

    def sql(statement):
        return command(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-Atqc", statement])

    def prove(path, user=None):
        relative = path.relative_to(root).as_posix()
        if relative in completed or relative in excluded:
            raise RuntimeError("unexpected or duplicate test: " + relative)
        # Upstream documents a race near the 30-second boundary. Start these
        # cases at a boundary; an actual failing test is never retried or ignored.
        if "30sec" in path.name:
            time.sleep(30 - time.time() % 30 + 0.1)
        output = command(["pg_prove", "-ovf", *(["-U", user] if user else []), str(path)])
        matches = re.findall(r"Files=1, Tests=(\d+)", output)
        if len(matches) != 1 or int(matches[0]) <= 0 or "Result: PASS" not in output:
            raise RuntimeError("missing successful TAP evidence: " + relative)
        counts.append(int(matches[0]))
        completed.add(relative)

    sql(
        "CREATE ROLE partman LOGIN; GRANT CREATE ON DATABASE postgres TO partman; "
        "GRANT ALL ON SCHEMA partman TO partman; "
        "GRANT ALL ON ALL TABLES IN SCHEMA partman TO partman; "
        "GRANT ALL ON ALL SEQUENCES IN SCHEMA partman TO partman"
    )
    for path in sorted(root.glob("*.sql")):
        prove(path, "partman" if "nonsuperuser" in path.name else None)
    sql("DROP OWNED BY partman; DROP ROLE partman")

    for directory in (
        "test_custom_time",
        "test_native",
        "test_native/test_default_child",
        "test_native/test_generated_col",
        "test_no_search_path",
        "test_upsert",
    ):
        for path in sorted((root / directory).glob("*.sql")):
            prove(path)

    tablespace = (Path.cwd() / "pg-maker-tablespace").resolve()
    tablespace.mkdir()
    sql("CREATE TABLESPACE mytablespace LOCATION '" + str(tablespace).replace("'", "''") + "'")
    prove(root / "test_native/test_tablespace/test-time-daily-native-tablespace-pg12.sql")
    sql("DROP TABLESPACE mytablespace")

    non_super = root / "test_native/test_nonsuperuser"
    prove(non_super / "test-nonsuperuser-part1.sql")
    prove(non_super / "test-id-nonsuperuser-part2.sql", "partman_owner")
    prove(non_super / "test-time-hourly-nonsuperuser-part2.sql", "partman_basic")
    prove(non_super / "test-nonsuperuser-part3.sql")

    for path in sorted((root / "test_procedure").glob("*.sql")):
        prove(path)
        for call in procedure_calls(path.read_text()):
            sql(call)

    connection = " ".join(
        key.lower()[2:] + "='" + os.environ[key].replace("'", "\\'") + "'"
        for key in ("PGHOST", "PGPORT", "PGUSER", "PGDATABASE")
    )
    connection = connection.replace("database=", "dbname=")
    for directory in ("test_reindex",):
        for path in sorted((root / directory).glob("*.sql")):
            prove(path)
            if path.name.startswith(("02-", "03-")):
                command(
                    [
                        "python3",
                        "bin/common/reapply_indexes.py",
                        "--parent",
                        "partman_reindex_test.test_reindex",
                        "--primary",
                        "--connection",
                        connection,
                        *(["--recreate_all"] if path.name.startswith("03-") else []),
                    ]
                )

    sql("ALTER SYSTEM SET pg_partman_bgw.dbname = 'postgres'")
    sql("SELECT pg_reload_conf()")
    for path in sorted((root / "test_bgw").glob("*.sql")):
        prove(path)
    sql("ALTER SYSTEM RESET pg_partman_bgw.dbname")
    sql("SELECT pg_reload_conf()")

    evidence = {
        "expected": sorted(expected),
        "completed": sorted(completed),
        "not_applicable": excluded,
        "assertions": sum(counts),
    }
    Path("pg-maker-tests.log").write_text(json.dumps(evidence, indent=2) + "\n")
    if completed != expected - excluded.keys():
        raise RuntimeError(
            "unexecuted upstream scripts: " + str(sorted(expected - excluded.keys() - completed))
        )
    print("PG_MAKER_PARTMAN_TESTS_PASSED=" + str(sum(counts)), flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 2:
        clock_fixture(sys.argv[1])
    else:
        main()
