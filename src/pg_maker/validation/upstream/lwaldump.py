"""Check durable WAL discovery on a real physical standby and across restart."""

import subprocess
import time
from pathlib import Path


def command(argv, check=True):
    result = subprocess.run(argv, capture_output=True, text=True)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    if check:
        result.check_returncode()
    return result


def sql(port, statement, check=True):
    return command(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(port),
            "-d",
            "postgres",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-Atqc",
            statement,
        ],
        check,
    )


def wait_for(port, query):
    for _ in range(120):
        if sql(port, query).stdout.strip() == "t":
            return
        time.sleep(0.25)
    raise RuntimeError("standby did not reach the expected WAL position")


def main():
    primary, standby = (Path("pg-maker-" + n).resolve() for n in ("primary", "standby"))
    started = []
    try:
        command(["initdb", "-D", str(primary), "-A", "trust", "--no-locale", "-E", "UTF8"])
        with (primary / "postgresql.conf").open("a") as f:
            f.write(
                "\nport=55439\nlisten_addresses='127.0.0.1'\n"
                "unix_socket_directories='/tmp'\nwal_keep_size='256MB'\n"
            )
        command(["pg_ctl", "-D", str(primary), "-l", str(primary / "server.log"), "-w", "start"])
        started.append(primary)
        sql(55439, "CREATE EXTENSION lwaldump; CREATE TABLE wal_probe(id int)")
        if sql(55439, "SELECT lwaldump()", check=False).returncode == 0:
            raise RuntimeError("lwaldump must reject a primary without a replay position")
        command(
            [
                "pg_basebackup",
                "-h",
                "127.0.0.1",
                "-p",
                "55439",
                "-D",
                str(standby),
                "-R",
                "-X",
                "stream",
            ]
        )
        with (standby / "postgresql.conf").open("a") as f:
            f.write("\nport=55440\n")
        command(["pg_ctl", "-D", str(standby), "-l", str(standby / "standby.log"), "-w", "start"])
        started.append(standby)
        wait_for(55440, "SELECT pg_is_in_recovery()")
        sql(55440, "SELECT pg_wal_replay_pause()")
        sql(55439, "INSERT INTO wal_probe SELECT generate_series(1,10000)")
        endpoint = sql(55439, "SELECT pg_current_wal_flush_lsn()").stdout.strip()
        wait_for(55440, f"SELECT pg_last_wal_receive_lsn() >= '{endpoint}'::pg_lsn")
        wait_for(55440, f"SELECT lwaldump() >= '{endpoint}'::pg_lsn")
        sql(55440, "SELECT pg_wal_replay_resume()")
        wait_for(55440, f"SELECT pg_last_wal_replay_lsn() >= '{endpoint}'::pg_lsn")
        wait_for(55440, "SELECT count(*)=10000 FROM wal_probe")
        command(["pg_ctl", "-D", str(standby), "-m", "immediate", "-w", "stop"])
        started.remove(standby)
        command(["pg_ctl", "-D", str(standby), "-l", str(standby / "standby.log"), "-w", "start"])
        started.append(standby)
        wait_for(55440, f"SELECT lwaldump() >= '{endpoint}'::pg_lsn")
        print("PG_MAKER_LWALDUMP_TESTS_PASSED=6", flush=True)
    finally:
        for data in reversed(started):
            command(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"], check=False)


if __name__ == "__main__":
    main()
