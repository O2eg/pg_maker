"""Recovery must validate bytes, preserve private mutable roots and honor retention."""

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pg_maker.errors import OwnershipError, PreconditionError, ValidationError
from pg_maker.execution.cache import SnapshotStore
from pg_maker.execution.checkpoints import Checkpoints
from pg_maker.execution.resources import check_resources
from pg_maker.storage import content_hash, write_json


@pytest.mark.parametrize("disabled", [False, True])
def test_final_only_checkpoints_preserve_final_snapshot(tmp_path, monkeypatch, disabled):
    if disabled:
        monkeypatch.setenv("PG_MAKER_NO_INTERMEDIATE_CHECKPOINTS", "1")
    else:
        monkeypatch.delenv("PG_MAKER_NO_INTERMEDIATE_CHECKPOINTS", raising=False)
    events = []
    root, output = tmp_path / "build", tmp_path / "output"
    for path in (root / "source", root / "prefix", output):
        path.mkdir(parents=True)
        (path / "file").write_text(path.name)
    branch = SimpleNamespace(
        id="fixture",
        root=root,
        source=root / "source",
        obj=root / "obj",
        output=output,
        context=SimpleNamespace(
            directory=tmp_path,
            state={"attempt": 1},
            check=lambda: None,
            event=lambda kind, **kw: events.append(kind),
        ),
        request=SimpleNamespace(
            cache_store=None,
            plan={"plan_hash": "fixture", "spec": {"run_db_tests": False}},
        ),
        config={
            "defaults": {
                "execution": {
                    "checkpoints": "all",
                    "limits": {
                        "min_free_disk_bytes": 0,
                        "memory_bytes": 1,
                    },
                }
            }
        },
    )
    steps = [{"id": "compile", "kind": "compile_core", "stage_key": "fixture", "cache": "always"}]
    checkpoints = Checkpoints(branch, steps)
    checkpoints.save(0)
    assert events == ([] if disabled else ["checkpoint_saved"])
    assert bool(list(checkpoints.index.glob("*.json"))) is (not disabled)
    checkpoints.save(0, complete=True)
    records = list(checkpoints.index.glob("*.json"))
    assert len(records) == 1
    import json

    record = json.loads(records[0].read_text())
    assert record["complete"]
    restored = tmp_path / "restored"
    checkpoints.store.restore(
        record["snapshot_hash"],
        {
            "source": restored / "source",
            "prefix": restored / "prefix",
            "output": restored / "output",
        },
    )
    assert (restored / "prefix/file").read_text() == "prefix"


def snapshot(tmp_path, **kwargs):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("original")
    (source / "file").chmod(0o755)
    (source / "link").symlink_to("file")
    store = SnapshotStore(tmp_path / "cache")
    document = store.capture({"root": source}, {"test": True}, **kwargs)
    return store, source, document


def test_snapshot_restore_is_independent_and_preserves_modes_symlinks(tmp_path):
    store, source, doc = snapshot(tmp_path)
    target = tmp_path / "restored"
    store.restore(doc["snapshot_hash"], {"root": target})
    assert (target / "file").read_text() == "original"
    assert (target / "file").stat().st_mode & 0o777 == 0o755
    assert (target / "link").is_symlink()
    (target / "file").write_text("private mutation")
    assert (source / "file").read_text() == "original"
    store.verify(doc["snapshot_hash"])
    with pytest.raises(PreconditionError, match="fresh"):
        store.restore(doc["snapshot_hash"], {"root": target})


def test_corrupted_cache_is_never_reused_and_capture_repairs_it(tmp_path):
    key = content_hash("key")
    store, source, doc = snapshot(tmp_path, key=key, producer={"run_id": "one"})
    blob = next((store.directory / "files").iterdir())
    blob.write_text("tampered")
    with pytest.raises(PreconditionError, match="changed"):
        store.lookup(key)
    assert not (tmp_path / "restore").exists()
    repaired = store.capture({"root": source}, {"test": True})
    assert repaired == doc
    store.verify(doc["snapshot_hash"])


def test_snapshot_manifest_path_escape_and_symlink_parent_rejected(tmp_path):
    store, source, doc = snapshot(tmp_path)
    bad = copy.deepcopy(doc)
    bad["entries"][0]["path"] = "../outside"
    bad["snapshot_hash"] = content_hash({k: v for k, v in bad.items() if k != "snapshot_hash"})
    write_json(store.directory / "snapshots" / bad["snapshot_hash"][7:], bad)
    with pytest.raises(PreconditionError, match="path"):
        store.restore(bad["snapshot_hash"], {"root": tmp_path / "target"})
    link = tmp_path / "parent-link"
    link.symlink_to(source)
    with pytest.raises(OwnershipError, match="symlink"):
        store.restore(doc["snapshot_hash"], {"root": link / "target"})
    assert not (source / "target").exists()


def test_snapshot_rejects_special_files_and_invalid_roots(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    store = SnapshotStore(tmp_path / "store")
    with pytest.raises(ValidationError):
        store.capture({"../outside": root}, {})
    os.mkfifo(root / "fifo")
    with pytest.raises(PreconditionError, match="special"):
        store.capture({"root": root}, {})


def test_prune_rechecks_pins_and_never_deletes_retained_operation_data(tmp_path):
    key = content_hash("one")
    store, source, doc = snapshot(tmp_path, key=key, producer={"run_id": "producer"})
    plan = store.prune_plan()
    assert plan["size_bytes"] > 0
    store.pin(tmp_path / "interrupted-operation", key)
    with pytest.raises(PreconditionError, match="changed"):
        store.prune(plan)
    assert store.prune_plan()["files"] == []
    store.verify(doc["snapshot_hash"])
    (source / "extra").write_text("orphan")
    store.capture({"root": source}, {})
    plan = store.prune_plan()
    assert plan["files"]
    store.prune(plan)
    store.verify(doc["snapshot_hash"])
    assert (source / "extra").read_text() == "orphan"


def test_private_checkpoints_cannot_be_pruned(tmp_path):
    store, _, _ = snapshot(tmp_path)
    write_json(store.directory / "checkpoint-store", {})
    with pytest.raises(PreconditionError, match="private"):
        store.prune_plan()


def test_snapshot_publication_follows_durable_file_batch(tmp_path, monkeypatch):
    import pg_maker.execution.cache as cache

    root = tmp_path / "source"
    root.mkdir()
    for number in range(12):
        (root / str(number)).write_text(str(number))
    observed = []
    sync, write = cache.sync_directory, cache.write_json

    def synced(path):
        observed.append(("sync", path.name))
        sync(path)

    def written(path, value):
        observed.append(("write", path.parent.name))
        write(path, value)

    monkeypatch.setattr(cache, "sync_directory", synced)
    monkeypatch.setattr(cache, "write_json", written)
    store = SnapshotStore(tmp_path / "store")
    store.capture({"root": root}, {}, key=content_hash("fixture"), producer={})
    assert observed.count(("sync", "files")) == 1
    assert observed.index(("sync", "files")) < observed.index(("write", "snapshots"))
    assert observed.index(("write", "snapshots")) < observed.index(("write", "entries"))


def test_interrupted_capture_syncs_existing_orphans_before_publishing(tmp_path, monkeypatch):
    import pg_maker.execution.cache as cache

    root = tmp_path / "source"
    root.mkdir()
    for number in range(4):
        (root / str(number)).write_text(str(number))
    store = SnapshotStore(tmp_path / "store")
    calls = 0

    def cancel_mid_copy():
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("interrupted before fsync")

    with pytest.raises(RuntimeError):
        store.capture({"root": root}, {}, check=cancel_mid_copy)
    orphan_files = set((store.directory / "files").iterdir())
    assert len(orphan_files) == 2 and not (store.directory / "snapshots").exists()
    synced = set()
    real_fsync = cache.os.fsync

    def fsync(fd):
        synced.add(Path(os.readlink("/proc/self/fd/" + str(fd))))
        real_fsync(fd)

    monkeypatch.setattr(cache.os, "fsync", fsync)
    document = store.capture({"root": root}, {})
    assert orphan_files <= synced
    store.verify(document["snapshot_hash"])


def test_resource_guard_fails_before_work_for_disk_cpu_and_memory(tmp_path, monkeypatch):
    import pg_maker.execution.resources as resources

    limits = {"min_free_disk_bytes": 100, "memory_bytes": 1024**3}
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda path: SimpleNamespace(free=99))
    with pytest.raises(PreconditionError, match="disk"):
        check_resources(tmp_path, limits)
    monkeypatch.setattr(resources.shutil, "disk_usage", lambda path: SimpleNamespace(free=150))
    with pytest.raises(PreconditionError, match="disk"):
        check_resources(tmp_path, limits, extra_bytes=51)
    monkeypatch.setattr(resources.os, "sched_getaffinity", lambda pid: {0})
    original = Path.read_text
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, **kw: (
            "MemAvailable: 1048576 kB\n" if str(p) == "/proc/meminfo" else original(p, **kw)
        ),
    )
    with pytest.raises(PreconditionError, match="CPUs"):
        check_resources(tmp_path, limits, jobs=2)
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, **kw: "MemAvailable: 1 kB\n" if str(p) == "/proc/meminfo" else original(p, **kw),
    )
    with pytest.raises(PreconditionError, match="memory"):
        check_resources(tmp_path, limits)


@pytest.mark.parametrize(
    "policy,expected", [("final", []), ("milestones", [1]), ("all", [0, 1, 2])]
)
def test_checkpoint_policy_excludes_component_objects_and_keeps_final(
    tmp_path, monkeypatch, policy, expected
):
    monkeypatch.delenv("PG_MAKER_NO_INTERMEDIATE_CHECKPOINTS", raising=False)
    root, output = tmp_path / "build", tmp_path / "output"
    for name in ("source", "obj", "prefix", "components/probe/target", "extra-sources", "clusters"):
        path = root / name
        path.mkdir(parents=True)
        (path / "file").write_text(name)
    output.mkdir()
    write_json(root / "workspace-journal.json", {"receipt": True})
    b = SimpleNamespace(
        id="fixture",
        root=root,
        source=root / "source",
        obj=root / "obj",
        output=output,
        workspace=True,
        context=SimpleNamespace(
            directory=tmp_path, state={"attempt": 1}, check=lambda: None, event=lambda *a, **k: None
        ),
        request=SimpleNamespace(
            cache_store=None, plan={"plan_hash": "plan", "spec": {"run_db_tests": True}}
        ),
        config={
            "defaults": {
                "execution": {
                    "checkpoints": policy,
                    "limits": {"min_free_disk_bytes": 0, "memory_bytes": 1},
                }
            }
        },
    )
    steps = [
        {"id": kind, "kind": kind, "stage_key": kind, "cache": "never"}
        for kind in ("compile_core", "install_components", "build_tools", "export")
    ]
    checkpoints = Checkpoints(b, steps)
    for i in range(3):
        checkpoints.save(i)
    records = [json.loads(p.read_text()) for p in checkpoints.index.glob("*.json")]
    assert sorted(r["index"] for r in records) == expected
    for r in records:
        doc = checkpoints.store.verify(r["snapshot_hash"])
        roots = {e["path"].split("/")[0] for e in doc["entries"]}
        assert roots <= {"source", "objects", "prefix", "output", "owners", "journal"}
        assert "journal" in roots
    checkpoints.save(3, complete=True)
    final = json.loads((checkpoints.index / "000001-000003.json").read_text())
    doc = checkpoints.store.verify(final["snapshot_hash"])
    assert {e["path"].split("/")[0] for e in doc["entries"]} == {"prefix", "output"}
