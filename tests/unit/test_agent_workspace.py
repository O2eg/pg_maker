import shutil

import pytest

from pg_maker.changes.apply import apply_patch
from pg_maker.changes.workspace import export_changes, workspace
from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.sources.store import InputStore, inventory
from pg_maker.storage import content_hash


def test_agent_export_round_trips_exact_changes_and_leaves_snapshot_unchanged(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "existing").write_text("old\nlast line")
    (original / "removed").write_text("remove me\n")
    store = InputStore(tmp_path / "inputs")
    digest = store.put_tree(original)
    work = tmp_path / "agent"
    workspace(store.root, digest, work)
    (work / "source/existing").write_text("new\nlast line")
    (work / "source/removed").unlink()
    (work / "source/added").write_text("new file\n")
    result = export_changes(work, tmp_path / "changes", "new-feature", ">=18.1,<19")
    assert result["acceptance_executed"] is False
    assert content_hash(inventory(original)) == digest
    replay = tmp_path / "replay"
    shutil.copytree(store.tree(digest), replay)
    apply_patch(replay, (tmp_path / "changes/changes.patch").read_text())
    assert inventory(replay) == inventory(work / "source")
    assert result["changed_files"] == ["added", "existing", "removed"]


def test_agent_refuses_mutated_snapshot_and_binary_export(tmp_path):
    original = tmp_path / "original"
    original.mkdir()
    (original / "text").write_text("text\n")
    store = InputStore(tmp_path / "inputs")
    digest = store.put_tree(original)
    work = tmp_path / "agent"
    workspace(store.root, digest, work)
    (work / "source/text").write_bytes(b"binary\0")
    with pytest.raises(UnsupportedError, match="binary"):
        export_changes(work, tmp_path / "changes", "feature", "==18.1")
    (store.tree(digest) / "text").write_text("tampered")
    with pytest.raises(PreconditionError, match="changed source tree"):
        export_changes(work, tmp_path / "changes", "feature", "==18.1")


def test_agent_workspace_cannot_be_nested_inside_cas(tmp_path):
    original = tmp_path / "source"
    original.mkdir()
    (original / "text").write_text("text")
    store = InputStore(tmp_path / "inputs")
    digest = store.put_tree(original)
    with pytest.raises(ValidationError, match="outside the immutable"):
        workspace(store.root, digest, store.tree(digest) / "nested-workspace")
    assert content_hash(inventory(store.tree(digest))) == digest
