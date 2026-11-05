"""Functional coverage for logerrors, whose pinned sources have no regression suite."""

import subprocess
import time


def sql(statement, expected=0):
    result = subprocess.run(
        ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-Atq"],
        input=statement,
        text=True,
        capture_output=True,
    )
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", flush=True)
    if result.returncode != expected:
        raise RuntimeError("unexpected psql exit status: " + str(result.returncode))
    return result.stdout.strip()


def main():
    sql("CREATE EXTENSION logerrors; SELECT pg_log_errors_reset();")
    # The API always retains WARNING/ERROR/FATAL TOTAL rows after reset.
    reset_check = (
        "SELECT count(*) = 3 AND bool_and(message = 'TOTAL' AND count = 0) "
        "FROM pg_log_errors_stats();"
    )
    if sql(reset_check) != "t":
        raise RuntimeError("reset did not clear error statistics")
    if sql("SELECT count(*) FROM pg_slow_log_stats();") != "1":
        raise RuntimeError("slow-log statistics interface returned an unexpected result")
    sql("SELECT 1/0;", expected=3)
    for _ in range(30):
        if (
            sql(
                "SELECT coalesce(sum(count),0) > 0 FROM pg_log_errors_stats() "
                "WHERE sqlstate = '22012';"
            )
            == "t"
        ):
            break
        time.sleep(1)
    else:
        raise RuntimeError("division-by-zero was not recorded by logerrors")
    sql("SELECT pg_log_errors_reset();")
    if sql(reset_check) != "t":
        raise RuntimeError("reset did not clear the recorded error")
    print("PG_MAKER_LOGERRORS_CHECKS_PASSED=4", flush=True)


if __name__ == "__main__":
    main()
