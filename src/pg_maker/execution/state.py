"""Journal-authoritative operation snapshots; callers serialize through control.lock."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from pg_maker.errors import StateError
from pg_maker.execution.events import append_event, read_events
from pg_maker.models import TERMINAL_STATUSES
from pg_maker.storage import utc_now, write_json


class OperationStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._state: dict[str, Any] | None = None

    def load(self, *, repair: bool = False) -> dict[str, Any]:
        _, last, _ = read_events(self.directory / "events.jsonl", repair=repair)
        if last is None:
            raise StateError("operation has no complete journal record")
        state = last["data"]["state"]
        if state["operation_id"] != self.directory.name:
            raise StateError("operation directory and journal identity differ")
        self._state = state
        if repair:
            write_json(self.directory / "state.json", state)
        return copy.deepcopy(state)

    def record(
        self,
        event_type: str,
        *,
        initial: dict[str, Any] | None = None,
        step_id: str | None = None,
        target_id: str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        if initial is not None:
            if (self.directory / "events.jsonl").exists():
                raise StateError("operation journal already exists")
            state = copy.deepcopy(initial)
            previous = None
        else:
            state = copy.deepcopy(
                self._state if self._state is not None else self.load(repair=True)
            )
            previous = state["status"]
        resuming = (
            event_type == "operation_resumed"
            and previous in {"failed", "cancelled", "interrupted", "partial"}
            and changes.get("status") == "planned"
            and changes.get("attempt") == state["attempt"] + 1
            and changes.get("owner") is None
            and not state["resources"]
        )
        if "status" in changes and previous in TERMINAL_STATUSES and not resuming:
            raise StateError("terminal operation status cannot be changed")
        if previous == "planned" and changes.get("status") == "succeeded":
            raise StateError("planned operation cannot directly succeed")
        state.update(changes)
        state["sequence"] += 1
        state["updated_at"] = utc_now()
        event = {
            "api_version": "pg_maker/run-event/v1",
            "kind": "BuildRunEvent",
            "sequence": state["sequence"],
            "timestamp": state["updated_at"],
            "run_id": None,
            "operation_id": state["operation_id"],
            "target_id": target_id,
            "step_id": step_id,
            "attempt_id": state["attempt"],
            "type": event_type,
            "data": {"state": state},
        }
        append_event(self.directory / "events.jsonl", event)
        self._state = state
        write_json(self.directory / "state.json", state)
        return copy.deepcopy(state)
