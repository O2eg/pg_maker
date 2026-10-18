"""Single-writer JSONL journal; only an unterminated final record may be discarded."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pg_maker.documents import validate_document
from pg_maker.errors import StateError, ValidationError
from pg_maker.storage import canonical_json, parse_json, private_open, sync_directory


def read_events(
    path: Path, *, after_sequence: int = 0, limit: int = 100, repair: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, int]:
    if (
        type(after_sequence) is not int
        or after_sequence < 0
        or type(limit) is not int
        or not 1 <= limit <= 1000
    ):
        raise ValidationError("invalid event cursor or limit (1..1000)")
    events, last, offset = [], None, 0
    if not path.exists():
        return events, last, offset
    with path.open("rb") as stream:
        for expected, line in enumerate(stream, 1):
            if not line.endswith(b"\n"):
                # A missing newline is the only repairable record; never skip a complete bad line.
                break
            try:
                event = parse_json(line)
                validate_document(event, "run-event")
                if event["sequence"] != expected or event["data"]["state"]["sequence"] != expected:
                    raise StateError("journal sequence is not contiguous")
                if last and event["operation_id"] != last["operation_id"]:
                    raise StateError("journal operation identity changed")
                if event["data"]["state"]["operation_id"] != event["operation_id"]:
                    raise StateError("event and state operation identities differ")
            except ValidationError as exc:
                raise StateError(
                    "invalid complete journal record", details={"sequence": expected}
                ) from exc
            if event["sequence"] > after_sequence and len(events) < limit:
                events.append(event)
            last = event
            offset += len(line)
    if repair and path.stat().st_size != offset:
        descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.ftruncate(descriptor, offset)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return events, last, offset


def append_event(path: Path, event: dict[str, Any]) -> None:
    validate_document(event, "run-event")
    with os.fdopen(private_open(path, append=True), "w", encoding="utf-8") as stream:
        stream.write(canonical_json(event) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)
