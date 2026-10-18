"""Detached worker entrypoint. An inherited flock descriptor owns the operation."""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import sys
from pathlib import Path

from pg_maker.execution.cancellation import CancellationToken
from pg_maker.execution.engine import execute_operation
from pg_maker.security import redact_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--owner-fd", type=int, required=True)
    args = parser.parse_args()
    token = CancellationToken(args.directory / "cancel.json")
    try:
        # A direct invocation must not bypass the owner's actual lock file.
        actual = os.fstat(args.owner_fd)
        expected = (args.directory / "owner.lock").stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RuntimeError("inherited owner descriptor does not match operation")
        fcntl.flock(args.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def request_stop(signum: int, frame: object) -> None:
            token.signalled = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        return execute_operation(args.directory, token)
    except BaseException as exc:
        print(redact_text(f"pg-maker worker: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 6
    finally:
        os.close(args.owner_fd)


if __name__ == "__main__":
    raise SystemExit(main())
