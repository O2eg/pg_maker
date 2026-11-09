"""Real filesystem transactions: failure isolation, removed files, and recovery."""

import os
from types import SimpleNamespace

import pytest

from pg_maker.errors import PreconditionError
from pg_maker.execution.install import ComponentInstall
from pg_maker.execution.workspace import compact_success
from pg_maker.sources.store import InputStore


def branch(tmp_path):
    root = tmp_path / "build"
    (root / "prefix/lib").mkdir(parents=True)
    (root / "prefix/lib/core.so").write_text("core")
    return SimpleNamespace(root=root, prefix="/opt/fixture/18", container_root="/work/build/test")


def stage(transaction, **files):
    for name, value in files.items():
        path = transaction.destination / "lib" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)


def test_failed_install_never_touches_shared_prefix_and_removed_files_disappear(tmp_path):
    b = branch(tmp_path)
    first = ComponentInstall(b, "extension").begin()
    stage(first, old="old version", retired="removed by next version")
    first.commit()
    failing = ComponentInstall(b, "extension").begin()
    stage(failing, old="incomplete replacement", partial="unfinished install")
    assert (b.root / "prefix/lib/old").read_text() == "old version"
    assert not (b.root / "prefix/lib/partial").exists()
    fixed = ComponentInstall(b, "extension").begin()
    stage(fixed, old="fixed")
    fixed.commit()
    assert (b.root / "prefix/lib/old").read_text() == "fixed"
    assert not (b.root / "prefix/lib/retired").exists()
    assert not (b.root / "prefix/lib/partial").exists()
    assert (b.root / "prefix/lib/core.so").read_text() == "core"


def test_publish_replays_intent_after_interruption(tmp_path, monkeypatch):
    b = branch(tmp_path)
    transaction = ComponentInstall(b, "extension").begin()
    stage(transaction, one="1", two="2")
    real = os.replace

    def interrupt(source, target):
        if str(target).endswith("/lib/two"):
            raise OSError("simulated disk interruption during publication")
        return real(source, target)

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(OSError, match="interruption"):
        transaction.commit()
    assert transaction.intent.exists()
    monkeypatch.setattr(os, "replace", real)
    ComponentInstall(b, "extension").recover()
    assert (b.root / "prefix/lib/one").read_text() == "1"
    assert (b.root / "prefix/lib/two").read_text() == "2"
    assert not transaction.intent.exists()


def test_component_cannot_overwrite_core_or_follow_install_symlink(tmp_path):
    b = branch(tmp_path)
    transaction = ComponentInstall(b, "extension").begin()
    stage(transaction, **{"core.so": "changed"})
    with pytest.raises(PreconditionError, match="conflicts"):
        transaction.commit()
    assert (b.root / "prefix/lib/core.so").read_text() == "core"
    transaction = ComponentInstall(b, "extension").begin()
    stage(transaction, **{"elsewhere/new": "changed"})
    (b.root / "prefix/lib/elsewhere").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PreconditionError, match="symlink"):
        transaction.commit()
    assert not (tmp_path / "new").exists()


def test_source_digest_cache_detects_same_size_same_mtime_tampering(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("good")
    store = InputStore(tmp_path / "inputs")
    digest = store.put_tree(source)
    target = store.tree(digest) / "file"
    before = target.stat()
    target.write_text("evil")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(PreconditionError, match="changed source tree"):
        store.tree(digest)


@pytest.mark.parametrize(
    ("goal", "retention", "retained"),
    [("package", "on-failure", False), ("build", "on-failure", True), ("package", "keep", True)],
)
def test_success_retention_keeps_only_requested_scratch(tmp_path, goal, retention, retained):
    b = branch(tmp_path)
    evidence = tmp_path / "output"
    evidence.mkdir()
    (evidence / "report").write_text("evidence")
    b.config = {"defaults": {"execution": {"workspace_retention": retention}}}
    b.request = SimpleNamespace(plan={"spec": {"goal": goal}})
    b.context = SimpleNamespace(event=lambda *args, **kwargs: None, directory=tmp_path)
    b.id = "fixture"
    validation = tmp_path / "validation/fixture/1/server/source"
    validation.mkdir(parents=True)
    (validation / "copied-package.deb").write_text("temporary package copy")
    compact_success(b)
    assert b.root.exists() is retained
    assert validation.exists() is retained
    assert (evidence / "report").read_text() == "evidence"


def test_retry_archives_test_evidence_instead_of_reusing_it(tmp_path):
    from pg_maker.execution.workspace import archive_evidence

    b = branch(tmp_path)
    b.output, b.id = tmp_path / "output", "fixture"
    b.context = SimpleNamespace(attempt_directory=tmp_path / "attempts/2")
    old = b.output / "components/probe"
    old.mkdir(parents=True)
    (old / "failed-test.log").write_text("failure details")
    archive_evidence(b, old)
    assert not old.exists()
    saved = list((tmp_path / "attempts/2/previous-evidence").rglob("failed-test.log"))
    assert len(saved) == 1 and saved[0].read_text() == "failure details"


def test_disabled_tests_exclude_clickhouse_and_test_only_dependencies(tmp_path):
    import yaml

    from pg_maker.assets import initialize_project
    from pg_maker.config import environment_requirements, load_config

    initialize_project(tmp_path, profile="yc-18.6-icu70", with_doc=False)
    path = tmp_path / "build.yaml"
    d = yaml.safe_load(path.read_text())
    d["spec"]["validation"]["run_db_tests"] = False
    path.write_text(yaml.safe_dump(d))
    config = load_config(path).data
    components = {
        c["metadata"]["id"]: c["spec"] for c in config["features"][0]["spec"]["components"]
    }
    assert "clickhouse" not in components["yc18-pg-clickhouse"]["extra_sources"]
    envs = environment_requirements(config)["environments"]
    assert [e["role"] for e in envs] == ["build"]
    assert "python3-pytest" not in envs[0]["packages"]
    assert not envs[0]["runtime_packages"]
    d["spec"]["validation"]["run_db_tests"] = True
    path.write_text(yaml.safe_dump(d))
    config = load_config(path).data
    components = {
        c["metadata"]["id"]: c["spec"] for c in config["features"][0]["spec"]["components"]
    }
    assert "clickhouse" in components["yc18-pg-clickhouse"]["extra_sources"]
    envs = environment_requirements(config)["environments"]
    assert {e["role"] for e in envs} == {"build", "test"}
    assert "python3-pytest" in next(e for e in envs if e["role"] == "build")["packages"]


def test_staging_has_header_directories_without_copying_core_files(tmp_path):
    b = branch(tmp_path)
    (b.root / "prefix/include/postgresql/server").mkdir(parents=True)
    transaction = ComponentInstall(b, "extension").begin()
    assert (transaction.destination / "include/postgresql/server").is_dir()
    assert not (transaction.destination / "lib/core.so").exists()
    with pytest.raises(PreconditionError, match="no staged files"):
        transaction.commit()


@pytest.mark.parametrize(
    "status,retained", [("failed", True), ("running", False), ("interrupted", False)]
)
def test_ambiguous_component_state_discards_only_its_objects(tmp_path, status, retained):
    from pg_maker.execution.workspace import WorkspaceJournal
    from pg_maker.planning.reuse import compilation_recipe

    b = branch(tmp_path)
    path = b.root / "components/probe"
    path.mkdir(parents=True)
    (path / "object.o").write_text("partial object")
    component = {
        "metadata": {"id": "probe"},
        "spec": {"type": "pgxs", "source": {"source_hash": "source"}, "requires": []},
    }
    journal = WorkspaceJournal.__new__(WorkspaceJournal)
    journal.branch, journal.path = b, b.root / "workspace-journal.json"
    journal.component_keys = {"probe": "key"}
    journal.data = {
        "components": {
            "probe": {
                "recipe": compilation_recipe(component),
                "dependencies": {},
                "status": status,
            }
        }
    }
    journal.prepare_component(component)
    assert (path / "object.o").exists() is retained
    assert (b.root / "prefix/lib/core.so").read_text() == "core"


def test_successful_test_receipts_never_skip_tests():
    from pg_maker.execution.workspace import WorkspaceJournal

    journal = WorkspaceJournal.__new__(WorkspaceJournal)
    journal.data = {"steps": {"test": {"key": "key", "status": "succeeded"}}}
    for kind in ("test_core", "test_source_world", "build_and_test_tools", "test_packages"):
        assert not journal.can_reuse(
            {"id": "test", "stage_key": "key", "kind": kind, "cache": "never"}
        )


def test_completed_checkpoint_preserves_artifact_and_test_evidence(tmp_path):
    from pg_maker.execution.build import BuildBranch
    from pg_maker.storage import read_json, write_json

    b = branch(tmp_path)
    b.obj, b.output = b.root / "obj", tmp_path / "output"
    b.report = {}
    b.request = SimpleNamespace(lock={"lock_hash": "lock"}, plan={"plan_hash": "plan"})
    expected = {
        "qualification": "package-verified",
        "artifact_manifest": "artifact-manifest.json",
        "package_tests": {"passed": True},
        "verified_test_ids": ["fresh-container"],
    }
    write_json(b.output / "report.json", expected)
    BuildBranch.begin(b, complete_checkpoint=True)
    assert all(b.report[k] == v for k, v in expected.items())
    assert read_json(b.output / "report.json") == expected
