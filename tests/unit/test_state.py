import copy

import pytest

from pg_maker.api import MakerService
from pg_maker.errors import StateError
from pg_maker.execution.events import read_events
from pg_maker.execution.state import OperationStore
from pg_maker.storage import canonical_json, content_hash, read_json, utc_now, write_json


def initial(directory, request_document):
    directory.mkdir()
    write_json(directory / "input.json", request_document)
    return {
        "api_version": "pg_maker/operation/v1",
        "kind": "OperationState",
        "operation_id": directory.name,
        "operation_kind": "container_commands",
        "input_hash": content_hash(request_document),
        "request_id": None,
        "status": "planned",
        "owner": None,
        "owner_token": "a" * 32,
        "attempt": 1,
        "sequence": 0,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "progress": {"completed": 0, "total": 1},
        "resources": [],
        "result": None,
        "error": None,
    }


def test_replay_repairs_stale_snapshot_and_torn_tail(tmp_path, request_document):
    directory = tmp_path / "op1"
    store = OperationStore(directory)
    first = store.record("created", initial=initial(directory, request_document))
    second = store.record("started", status="running")
    write_json(directory / "state.json", first)
    with (directory / "events.jsonl").open("ab") as stream:
        stream.write(b'{"sequence":3')
    recovered = OperationStore(directory).load(repair=True)
    assert recovered == second == read_json(directory / "state.json")
    assert (directory / "events.jsonl").read_bytes().endswith(b"\n")


@pytest.mark.parametrize("damage", [b"not json\n", b"{}\n", b"\n"])
def test_complete_bad_journal_record_never_discarded(tmp_path, request_document, damage):
    directory = tmp_path / "op1"
    OperationStore(directory).record("created", initial=initial(directory, request_document))
    path = directory / "events.jsonl"
    path.write_bytes(path.read_bytes() + damage)
    original = path.read_bytes()
    with pytest.raises(StateError):
        OperationStore(directory).load(repair=True)
    assert path.read_bytes() == original


def test_sequence_gap_fails_and_cursor_pages(tmp_path, request_document):
    directory = tmp_path / "op1"
    store = OperationStore(directory)
    store.record("created", initial=initial(directory, request_document))
    store.record("started", status="running")
    store.record("finished", status="succeeded")
    service = MakerService(tmp_path)
    page = service.events(directory, limit=2)
    assert [e["sequence"] for e in page["events"]] == [1, 2]
    assert page["has_more"] and page["last_sequence"] == 2
    page = service.events(directory, after_sequence=2)
    assert len(page["events"]) == 1 and not page["has_more"]
    events, _, _ = read_events(directory / "events.jsonl")
    bad = copy.deepcopy(events[-1])
    bad["sequence"] = 8
    with (directory / "events.jsonl").open("a") as stream:
        stream.write(canonical_json(bad) + "\n")
    with pytest.raises(StateError):
        service.status(directory)


def test_terminal_state_cannot_change_and_lost_worker_is_interrupted(tmp_path, request_document):
    directory = tmp_path / "op1"
    store = OperationStore(directory)
    store.record("created", initial=initial(directory, request_document))
    state = MakerService(tmp_path).status(directory)
    assert state["status"] == "interrupted"
    with pytest.raises(StateError):
        OperationStore(directory).record("retry", status="running")


def test_snapshot_failure_replays_already_durable_event(tmp_path, request_document, monkeypatch):
    directory = tmp_path / "op1"
    store = OperationStore(directory)
    store.record("created", initial=initial(directory, request_document))

    def fail(*args):
        raise OSError("simulated snapshot failure")

    with monkeypatch.context() as patch:
        patch.setattr("pg_maker.execution.state.write_json", fail)
        with pytest.raises(OSError):
            store.record("started", status="running")
    state = OperationStore(directory).load(repair=True)
    assert state["sequence"] == 2 and state["status"] == "running"


def test_journal_failure_cannot_advance_snapshot(tmp_path, request_document, monkeypatch):
    directory = tmp_path / "op1"
    store = OperationStore(directory)
    first = store.record("created", initial=initial(directory, request_document))

    def fail(*args):
        raise OSError("simulated journal failure")

    monkeypatch.setattr("pg_maker.execution.state.append_event", fail)
    with pytest.raises(OSError):
        store.record("started", status="running")
    assert read_json(directory / "state.json") == first
