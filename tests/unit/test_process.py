import os
import signal
import sys
import time

import pytest

from pg_maker.execution.cancellation import process_identity, process_is_alive, stop_process_group
from pg_maker.execution.process import run_process


def test_large_output_is_streamed_and_tails_bounded(tmp_path):
    result = run_process(
        [sys.executable, "-c", "import sys; print('x'*4000000); print('err',file=sys.stderr)"],
        log_directory=tmp_path,
        name="large",
        timeout_seconds=10,
    )
    assert result.succeeded
    assert len(result.stdout_tail) <= 16384
    assert (tmp_path / "large.stdout.log").stat().st_size == 4000001
    assert result.stderr_tail == "err\n"
    assert (tmp_path / "large.stdout.log").stat().st_mode & 0o777 == 0o600


def test_secret_split_by_writes_and_utf8(tmp_path):
    code = (
        "import os,time; os.write(1, 'Привет secret-'.encode()); "
        "time.sleep(.1); os.write(1,b'value end')"
    )
    result = run_process(
        [sys.executable, "-c", code],
        log_directory=tmp_path,
        name="secret",
        secrets=("secret-value",),
        timeout_seconds=10,
    )
    assert result.stdout_tail == "Привет [REDACTED] end"
    assert "secret-value" not in (tmp_path / "secret.stdout.log").read_text()


@pytest.mark.parametrize("parent_exits", [False, True])
def test_timeout_kills_stubborn_descendant(tmp_path, parent_exits):
    pid_file = tmp_path / "pid"
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    code = (
        "import subprocess,sys,time,pathlib; "
        f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        + ("sys.exit(0)" if parent_exits else "time.sleep(30)")
    )
    result = run_process(
        [sys.executable, "-c", code],
        log_directory=tmp_path,
        name="timeout",
        timeout_seconds=1,
        grace_seconds=0.1,
    )
    pid = int(pid_file.read_text())
    try:
        assert result.timed_out
        deadline = time.monotonic() + 2
        while process_identity(pid) is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert process_identity(pid) is None
    finally:
        if process_identity(pid) is not None:
            os.kill(pid, signal.SIGKILL)


def test_cancellation_and_existing_logs(tmp_path):
    start = time.monotonic()
    result = run_process(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        log_directory=tmp_path,
        name="cancel",
        timeout_seconds=10,
        cancelled=lambda: time.monotonic() - start > 0.2,
        grace_seconds=0.1,
    )
    assert result.cancelled and not result.timed_out
    original = (tmp_path / "cancel.stdout.log").read_bytes()
    with pytest.raises(FileExistsError):
        run_process(["true"], log_directory=tmp_path, name="cancel")
    assert (tmp_path / "cancel.stdout.log").read_bytes() == original


def test_pid_reuse_does_not_signal_current_process():
    identity = process_identity(os.getpid())
    assert process_is_alive(identity)
    stale = {**identity, "start_ticks": identity["start_ticks"] - 1}
    assert not process_is_alive(stale)
    stop_process_group(stale, grace_seconds=0.01)
