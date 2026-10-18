"""Explicit cancellation and Linux process identity, including boot and start time."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from pg_maker.errors import OwnershipError


def process_identity(pid: int) -> dict[str, Any] | None:
    try:
        directory = Path(f"/proc/{pid}")
        stat = (directory / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] in {"Z", "X", "x"}:
            return None
        return {
            "pid": pid,
            "start_ticks": int(stat[19]),
            "uid": directory.stat().st_uid,
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (OSError, ValueError, IndexError):
        return None


def process_is_alive(identity: dict[str, Any] | None) -> bool:
    return bool(identity and identity == process_identity(identity["pid"]))


def stop_process_group(identity: dict[str, Any], *, grace_seconds: float = 2.0) -> None:
    if identity.get("uid") != os.getuid():
        raise OwnershipError("refusing to signal a process owned by another uid")
    pid = identity["pid"]
    if not process_is_alive(identity):
        return
    if os.getpgid(pid) != pid:
        raise OwnershipError("recorded process is not a process-group leader")
    try:
        os.killpg(pid, signal.SIGTERM)
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            time.sleep(0.02)
        # A recovered process is not our unreaped child; recheck identity before escalation.
        if process_is_alive(identity):
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class CancellationToken:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.signalled = False

    def requested(self) -> bool:
        return self.signalled or self.path.exists()
