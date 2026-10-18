"""Append-only human progress, independent of the authoritative event journal."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TextIO

from pg_maker.security import redact_text
from pg_maker.storage import private_open, utc_now


class ProgressReporter:
    def __init__(
        self,
        path: Path,
        *,
        operation_id: str,
        target_id: str | None = None,
        attempt: int = 1,
        stream: TextIO | None = None,
    ) -> None:
        self._file = os.fdopen(private_open(path, append=True), "w", encoding="utf-8")
        self._stream = stream if stream is not None else sys.stderr
        self.operation_id, self.target_id, self.attempt = operation_id, target_id, attempt
        self.total, self.completed = 1, 0

    def configure(self, total: int) -> None:
        self.total, self.completed = max(1, total), 0

    def info(self, message: str, *, step_id: str | None = None) -> None:
        self._emit("INFO", message, step_id)

    def error(self, message: str, *, step_id: str | None = None) -> None:
        self._emit("ERROR", message, step_id)

    def advance(self, message: str, *, step_id: str | None = None) -> None:
        self.completed = min(self.total, self.completed + 1)
        self.info(message, step_id=step_id)

    def _emit(self, level: str, message: str, step_id: str | None) -> None:
        message = " ".join(redact_text(message).splitlines())
        line = (
            f"{utc_now()} level={level} operation={self.operation_id} "
            f"target={self.target_id or '-'} step={step_id or '-'} attempt={self.attempt} "
            f"progress={self.completed * 100 // self.total}% {message}\n"
        )
        self._file.write(line)
        self._file.flush()
        try:
            self._stream.write(line)
            self._stream.flush()
        except BrokenPipeError:
            pass

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> ProgressReporter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
