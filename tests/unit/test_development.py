"""Real Git operations and frozen plans; no PostgreSQL regression suites."""

import copy
import os
from pathlib import Path

import pytest

from pg_maker.development.git import PatchStack, git
from pg_maker.development.session import DevelopmentSession
from pg_maker.errors import PreconditionError
from pg_maker.execution.cache import SnapshotStore
from pg_maker.execution.incremental import sync_sources
from pg_maker.planning.documents import seal
from pg_maker.planning.plan import build_plan
from pg_maker.sources.store import inventory
from pg_maker.storage import read_json
from pg_maker.yamlio import load_yaml
from tests.unit.test_planning import project as project_fixture
from tests.unit.test_planning import resolved, write_yaml


@pytest.fixture
def project(tmp_path):
    return project_fixture.__wrapped__(tmp_path)


def stack_fixture(tmp_path):
    source = tmp_path / "upstream"
    source.mkdir()
    (source / "file").write_text("upstream\n")
    stack = PatchStack(tmp_path / "owned")
    stack.initialize(source)
    return source, stack


def test_refresh_middle_patch_replays_tail_and_pop_push_keep_history(tmp_path):
    _, stack = stack_fixture(tmp_path)
    (stack.path / "file").write_text("first\n")
    stack.commit("first")
    (stack.path / "other").write_text("second\n")
    second = stack.commit("second")["entries"][-1]["commit"]
    stack.move("pop")
    (stack.path / "file").write_text("improved first\n")
    refreshed = stack.refresh()
    assert refreshed["pending"] is None
    assert refreshed["entries"][-1]["commit"] != second
    assert (stack.path / "other").read_text() == "second\n"
    stack.move("pop", 2)
    assert (stack.path / "file").read_text() == "upstream\n"
    assert not (stack.path / "other").exists()
    stack.move("push", 2)
    assert (stack.path / "file").read_text() == "improved first\n"
    exported = stack.export(tmp_path / "series")
    assert [p["id"] for p in exported["patches"]] == ["first", "second"]
    assert "Subject:" in (tmp_path / "series" / exported["patches"][0]["file"]).read_text()


@pytest.mark.parametrize("finish", ["continue", "abort"])
def test_rebase_conflict_has_recoverable_state(tmp_path, finish):
    source, stack = stack_fixture(tmp_path)
    (stack.path / "file").write_text("our patch\n")
    original = stack.commit("ours")
    (source / "file").write_text("new upstream\n")
    state = stack.rebase(source)
    assert state["pending"]["conflict"]
    assert b"<<<<<<<" in (stack.path / "file").read_bytes()
    with pytest.raises(PreconditionError, match="pending"):
        stack.move("pop")
    if finish == "continue":
        (stack.path / "file").write_text("resolved implementation\n")
        result = stack.continue_operation()
        assert result["pending"] is None and result["cursor"] == 1
        assert (stack.path / "file").read_text() == "resolved implementation\n"
    else:
        result = stack.abort()
        assert result == original
        assert (stack.path / "file").read_text() == "our patch\n"


def test_dirty_checkout_is_not_silently_discarded_and_manual_commit_can_be_adopted(tmp_path):
    _, stack = stack_fixture(tmp_path)
    (stack.path / "file").write_text("manual\n")
    with pytest.raises(PreconditionError):
        stack.move("pop")
    git(stack.path, "add", "-A")
    git(stack.path, "commit", "-m", "Manual development commit")
    with pytest.raises(PreconditionError, match="adopt"):
        stack.move("pop")
    stack.adopt()
    stack.move("pop")
    assert (stack.path / "file").read_text() == "upstream\n"


def test_import_format_patch_and_preserve_original_repository(tmp_path):
    source, stack = stack_fixture(tmp_path)
    (stack.path / "file").write_text("local feature\n")
    stack.commit("feature")
    export = stack.export(tmp_path / "export")
    clone = PatchStack(tmp_path / "clone")
    old_head = stack.head()
    clone.initialize(source, repository=stack.path)
    assert git(clone.path, "rev-list", "--count", "HEAD").stdout.strip() == b"3"
    clone.import_patch(tmp_path / "export" / export["patches"][0]["file"], "imported")
    assert (clone.path / "file").read_text() == "local feature\n"
    assert stack.head() == old_head


@pytest.mark.parametrize("finish", ["continue", "abort"])
def test_import_series_keeps_queue_across_conflict(tmp_path, finish):
    source, stack = stack_fixture(tmp_path)
    (stack.path / "file").write_text("patch one\n")
    stack.commit("one")
    (stack.path / "second").write_text("patch two\n")
    stack.commit("two")
    exported = stack.export(tmp_path / "export")
    (source / "file").write_text("changed base\n")
    other = PatchStack(tmp_path / "other")
    before = other.initialize(source)
    state = other.import_series(
        [(tmp_path / "export" / p["file"], p["id"]) for p in exported["patches"]]
    )
    assert len(state["pending"]["queue"]) == 2
    if finish == "continue":
        (other.path / "file").write_text("resolved patch\n")
        result = other.continue_operation()
        assert result["pending"] is None
        assert [e["id"] for e in result["entries"]] == ["one", "two"]
        assert (other.path / "second").read_text() == "patch two\n"
    else:
        other.abort()
        assert other.load() == before
        assert (other.path / "file").read_text() == "changed base\n"


def test_rebase_preserves_patch_already_present_upstream(tmp_path):
    source, stack = stack_fixture(tmp_path)
    (stack.path / "file").write_text("upstream fix\n")
    stack.commit("upstream-fix")
    (source / "file").write_text("upstream fix\n")
    state = stack.rebase(source)
    assert state["pending"] is None and state["cursor"] == 1


def components_project(project):
    feature_path = project / "config/features/my-feature.yaml"
    feature = load_yaml(feature_path)
    feature["spec"]["components"] = []
    for identifier, dependencies in (("one", []), ("two", []), ("dependent", ["two"])):
        source = project / identifier
        source.mkdir()
        (source / "extension.c").write_text("int result = 1;\n")
        component = {
            "api_version": "pg_maker/component/v1",
            "kind": "ComponentRecipe",
            "metadata": {"id": identifier, "revision": 1},
            "spec": {
                "type": "pgxs",
                "source": {
                    "kind": "snapshot",
                    "path": "../" + identifier,
                    "include": ["**"],
                    "exclude": [],
                },
                "compatible_postgresql": ">=18,<19",
                "requires": dependencies,
                "conflicts": [],
                "build_dependencies": [],
                "runtime_dependencies": [],
                "make_targets": ["all"],
                "install_targets": ["install"],
                "test_targets": [],
                "required_tests": [],
                "runtime_files": ["lib/" + identifier + ".so"],
            },
        }
        write_yaml(project / "components" / (identifier + ".yaml"), component)
        feature["spec"]["components"].append("../../components/" + identifier + ".yaml")
    write_yaml(feature_path, feature)
    state, locked = resolved(project)
    assert state["status"] == "succeeded", state
    return locked


def test_component_edit_preserves_core_and_independent_keys_but_invalidates_dependents(project):
    locked = components_project(project)
    session = DevelopmentSession(project / "development")
    status = session.open(lock=project / "lock.json")
    first = session.explain()["plan"]
    source = Path(status["trees"]["component:two"]["source"])
    (source / "extension.c").write_text("int result = 2;\n")
    second = session.explain()["plan"]
    old = {s["id"]: s for s in first["spec"]["steps"]}
    new = {s["id"]: s for s in second["spec"]["steps"]}
    for identifier, step in new.items():
        if step["kind"] in {
            "configure",
            "compile_core",
            "install_components",
        } or identifier.endswith("component-one"):
            assert step["stage_key"] == old[identifier]["stage_key"]
        if identifier.endswith(("component-two", "component-dependent")):
            assert step["stage_key"] != old[identifier]["stage_key"]
    selected = session.explain(components=["dependent"])["plan"]
    assert [
        s["parameters"]["component_id"]
        for s in selected["spec"]["steps"]
        if s["kind"] == "build_tools"
    ] == ["two", "dependent"]
    # Header changes invalidate every component, including independent ones.
    core = Path(status["trees"]["postgresql"]["source"])
    (core / "src/include/new.h").write_text("#define CHANGED_ABI 1\n")
    third = session.explain()["plan"]
    assert all(
        s["stage_key"] != new[s["id"]]["stage_key"]
        for s in third["spec"]["steps"]
        if s["kind"] in {"compile_core", "build_tools"}
    )
    assert read_json(project / "lock.json") == locked


def test_incremental_sync_preserves_unchanged_objects_and_timestamps(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.c").write_text("old a")
    (source / "b.c").write_text("unchanged b")
    (source / "retired.c").write_text("retired")
    import shutil

    work = tmp_path / "work"
    shutil.copytree(source, work)
    (work / "a.o").write_text("old object")
    (work / "b.o").write_text("reusable object")
    os.utime(work / "b.c", (10, 10))
    old = inventory(source)["entries"]
    (source / "a.c").write_text("fixed a")
    (source / "retired.c").unlink()
    sync_sources(work, source, old)
    assert (work / "b.c").stat().st_mtime == 10
    assert (work / "b.o").read_text() == "reusable object"
    assert (work / "a.c").stat().st_mtime_ns > (work / "a.o").stat().st_mtime_ns
    assert not (work / "retired.c").exists()
    cache = SnapshotStore(tmp_path / "cache")
    snapshot = cache.capture({"root": work}, {})
    cache.restore(snapshot["snapshot_hash"], {"root": tmp_path / "restored"})
    assert (tmp_path / "restored/b.c").stat().st_mtime == 10


def test_toolchain_changes_invalidate_all_component_keys(project):
    lock = components_project(project)
    before = build_plan(lock)
    changed = copy.deepcopy(lock["spec"])
    changed["config"]["variants"][0]["build"]["resolved_flags"]["cflags"].append(
        "-fno-omit-frame-pointer"
    )
    after = build_plan(seal("lock", changed, lock["transport"]))
    a = {s["id"]: s for s in before["spec"]["steps"]}
    assert all(
        s["stage_key"] != a[s["id"]]["stage_key"]
        for s in after["spec"]["steps"]
        if s["kind"] in {"compile_core", "build_tools"}
    )
