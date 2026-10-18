"""Bounded streaming subprocesses. Host execution is for controller tools only."""

from __future__ import annotations

import codecs
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.execution.cancellation import process_identity
from pg_maker.models import CommandResult
from pg_maker.security import StreamRedactor, environment_secrets
from pg_maker.storage import private_open


def run_process(
    argv: Sequence[str],
    *,
    log_directory: Path,
    name: str,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout_seconds: int = 60,
    cancelled: Callable[[], bool] = lambda: False,
    secrets: tuple[str, ...] = (),
    grace_seconds: float = 1.0,
    on_start: Callable[[dict], None] | None = None,
    pass_fds: tuple[int, ...] = (),
) -> CommandResult:
    if (
        not argv
        or isinstance(argv, str)
        or any(not isinstance(x, str) or "\x00" in x for x in argv)
    ):
        raise ValidationError("argv must be a non-empty sequence of strings without NUL")
    if type(timeout_seconds) is not int or timeout_seconds <= 0:
        raise ValidationError("timeout_seconds must be a positive integer")
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValidationError("invalid command log name")
    if cancelled():
        from pg_maker.errors import CancelledError

        raise CancelledError("operation cancelled before process start")
    paths = [log_directory / f"{name}.stdout.log", log_directory / f"{name}.stderr.log"]
    streams = []
    process = None
    identity = None
    selector = selectors.DefaultSelector()
    start = time.monotonic()
    tails = ["", ""]
    timed_out = was_cancelled = False
    decoders = [codecs.getincrementaldecoder("utf-8")("replace") for _ in paths]
    redactors = [StreamRedactor(secrets + environment_secrets()) for _ in paths]

    def write(index: int, payload: bytes, *, final: bool = False) -> None:
        text = redactors[index].feed(decoders[index].decode(payload, final=final), final=final)
        streams[index].write(text)
        streams[index].flush()
        tails[index] = (tails[index] + text)[-16384:]

    def exited() -> bool:
        assert process is not None
        # WNOWAIT keeps the leader's PID reserved, including after an early parent exit.
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    def stop_owned_group() -> None:
        assert process is not None
        try:
            os.killpg(process.pid, signal.SIGTERM)
            time.sleep(grace_seconds)
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    try:
        for path in paths:
            streams.append(os.fdopen(private_open(path, exclusive=True), "w", encoding="utf-8"))
        try:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                start_new_session=True,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            raise PreconditionError(f"cannot start executable: {argv[0]}") from exc
        identity = process_identity(process.pid)
        if identity is not None and on_start is not None:
            on_start(identity)
        for index, pipe in enumerate((process.stdout, process.stderr)):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, index)
        stopped_at: float | None = None
        while selector.get_map() or not exited():
            now = time.monotonic()
            if stopped_at is None and (cancelled() or now - start >= timeout_seconds):
                was_cancelled = cancelled()
                timed_out = not was_cancelled
                stop_owned_group()
                stopped_at = time.monotonic()
            for key, _ in selector.select(0.05):
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if data:
                    write(key.data, data)
                else:
                    selector.unregister(key.fileobj)
                    write(key.data, b"", final=True)
            if stopped_at is not None and time.monotonic() - stopped_at > 2:
                for key in list(selector.get_map().values()):
                    selector.unregister(key.fileobj)
                    write(key.data, b"", final=True)
                break
        # Even descendants which closed inherited pipes must not outlive this command.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        code = process.wait(timeout=5)
        for stream in streams:
            stream.flush()
            os.fsync(stream.fileno())
        return CommandResult(
            code,
            str(paths[0]),
            str(paths[1]),
            tails[0],
            tails[1],
            int((time.monotonic() - start) * 1000),
            timed_out,
            was_cancelled,
        )
    finally:
        if process is not None:
            if process.returncode is None:
                stop_owned_group()
                process.wait(timeout=5)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()
        selector.close()
        for stream in streams:
            stream.close()
