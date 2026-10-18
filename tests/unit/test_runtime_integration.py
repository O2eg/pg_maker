"""Runtime package identity, worker contract and cleanup failure boundaries."""

import copy
from types import SimpleNamespace

import pytest

from pg_maker.artifacts.manifest import export_operation
from pg_maker.errors import PreconditionError
from pg_maker.execution.cleanup import apply_cleanup, cleanup_plan
from pg_maker.integrations.machine import envelope
from pg_maker.integrations.runtime import plan_runtime, verify_runtime_plan
from pg_maker.models import RuntimeRequest, parse_request
from pg_maker.storage import content_hash, file_hash, read_json, write_json

EXTENSIONS = ("pg_stat_statements", "pg_buffercache", "pg_wait_sampling", "pg_stat_kcache")


def runtime_fixture(tmp_path, monkeypatch):
    write_json(tmp_path / "evidence.json", {})
    packages = []
    for role in ("server", "client", "libpq"):
        path = tmp_path / (role + ".deb")
        path.write_bytes(role.encode())
        packages.append(
            {
                "path": path.name,
                "sha256": file_hash(path),
                "package_role": role,
                "producer": {"plan_hash": "sha256:build"},
                "feature": "custom",
                "target_id": "debian13-x64",
                "source_hash": "sha256:source",
                "install_tree_hash": "sha256:tree",
                "os": {"id": "debian", "version": "13"},
                "architecture": "x86_64",
                "postgresql_version": "18.1",
                "qualification": "unverified",
                "verification": {
                    "path": "evidence.json",
                    "sha256": file_hash(tmp_path / "evidence.json"),
                },
            }
        )
    manifest = {"artifacts": packages}
    manifest["manifest_hash"] = content_hash(manifest)
    write_json(tmp_path / "artifact-manifest.json", manifest)
    entries = [
        "opt/custom/18/bin/postgres",
        *["opt/custom/18/lib/" + x + ".so" for x in (*EXTENSIONS, "auto_explain")],
    ]
    monkeypatch.setattr(
        "pg_maker.integrations.runtime.inspect_deb",
        lambda _: {"entries": [{"path": p} for p in entries]},
    )
    entries.extend(
        "opt/custom/18/share/postgresql/extension/" + name + suffix
        for name in EXTENSIONS
        for suffix in (".control", "--1.0.sql")
    )
    repo = tmp_path / "runtime-repository"
    repo.mkdir()
    (repo / "Packages").write_text("")
    records = [{"path": "Packages", "sha256": file_hash(repo / "Packages")}]
    write_json(
        tmp_path / "runtime-repository.json",
        {"files": records, "repository_hash": content_hash(records)},
    )
    files = {}
    for name in ("docker-entrypoint.sh", "pg-stand-entrypoint", "pg-stand-pgbouncer"):
        path = tmp_path / name
        path.write_text("#!/bin/sh\nexit 0\n")
        files[name] = {"path": str(path), "sha256": file_hash(path)}
    return {
        "base_image": "sha256:" + "a" * 64,
        "files": files,
        "entrypoint": "pg-stand-entrypoint",
        "extensions": list(EXTENSIONS),
        "libraries": ["auto_explain"],
        "min_free_disk_bytes": 1,
        "memory_bytes": 1000000,
    }, entries


def test_runtime_plan_rejects_changed_package_or_profile_and_round_trips(tmp_path, monkeypatch):
    profile, _ = runtime_fixture(tmp_path, monkeypatch)
    plan = plan_runtime(tmp_path, profile)
    assert envelope("runtime plan", "test", result=plan)["result"] == plan
    assert (
        parse_request(RuntimeRequest(plan, str(tmp_path / "runtime.json")).to_document()).plan
        == plan
    )
    verify_runtime_plan(plan, plan["plan_hash"])
    (tmp_path / "pg-stand-entrypoint").write_text("changed")
    with pytest.raises(PreconditionError, match="profile file hash"):
        verify_runtime_plan(plan, plan["plan_hash"])
    (tmp_path / "server.deb").write_text("changed package")
    with pytest.raises(PreconditionError, match="artifact hash"):
        plan_runtime(tmp_path, profile)


def test_external_extensions_must_be_in_custom_prefix(tmp_path, monkeypatch):
    profile, entries = runtime_fixture(tmp_path, monkeypatch)
    entries.remove("opt/custom/18/lib/pg_stat_kcache.so")
    entries.append("usr/lib/postgresql/18/lib/pg_stat_kcache.so")
    with pytest.raises(PreconditionError, match="lacks extensions"):
        plan_runtime(tmp_path, profile)


@pytest.mark.parametrize("suffix", [".control", "--1.0.sql"])
def test_runtime_plan_requires_control_and_install_sql(tmp_path, monkeypatch, suffix):
    profile, entries = runtime_fixture(tmp_path, monkeypatch)
    entries.remove("opt/custom/18/share/postgresql/extension/pg_stat_statements" + suffix)
    entries.append("opt/custom/18/share/postgresql/extension/pg_stat_statements--0.9--1.0.sql")
    with pytest.raises(PreconditionError, match="control or install"):
        plan_runtime(tmp_path, profile)


def test_runtime_repository_must_be_complete_and_symlink_free(tmp_path, monkeypatch):
    profile, _ = runtime_fixture(tmp_path, monkeypatch)
    extra = tmp_path / "runtime-repository/extra"
    extra.symlink_to("/etc/passwd")
    with pytest.raises(PreconditionError, match="inventory"):
        plan_runtime(tmp_path, profile)
    extra.unlink()
    (tmp_path / "runtime-repository.json").unlink()
    with pytest.raises(PreconditionError, match="missing pinned"):
        plan_runtime(tmp_path, profile)


def exported_operation(tmp_path):
    directory = tmp_path / "operation"
    directory.mkdir()
    state = {"operation_id": "operation", "status": "succeeded", "resources": [], "result": None}
    write_json(directory / "state.json", state)
    write_json(directory / "input.json", {"operation_kind": "container_commands"})
    for name in ("workspace/build", "checkpoints", "workspace/output"):
        (directory / name).mkdir(parents=True)
        (directory / name / "keep").write_text(name)
    exported = tmp_path / "export"
    export_operation(directory, exported, include_inputs=False)
    service = SimpleNamespace(_reference=lambda p: p, status=lambda p: read_json(p / "state.json"))
    return directory, exported, service


def test_cleanup_removes_only_private_scratch_after_verified_export(tmp_path):
    directory, exported, service = exported_operation(tmp_path)
    plan = cleanup_plan(service, directory, exported)
    result = apply_cleanup(service, plan)
    assert result["removed"] == ["workspace/build", "checkpoints"]
    assert not (directory / "workspace/build").exists()
    assert (directory / "workspace/output/keep").read_text() == "workspace/output"
    assert (exported / "output/keep").is_file()
    assert (directory / "scratch-cleaned.json").is_file()


def test_cleanup_rejects_changed_export_and_arbitrary_path(tmp_path):
    directory, exported, service = exported_operation(tmp_path)
    plan = cleanup_plan(service, directory, exported)
    changed = copy.deepcopy(plan)
    changed["entries"][0]["path"] = "output"
    changed["plan_hash"] = content_hash({k: v for k, v in changed.items() if k != "plan_hash"})
    with pytest.raises(PreconditionError, match="outside private scratch"):
        apply_cleanup(service, changed)
    (exported / "output/keep").write_text("changed")
    with pytest.raises(PreconditionError, match="inventory"):
        apply_cleanup(service, plan)
    assert (directory / "workspace/build/keep").is_file()


def test_cleanup_refuses_parent_symlink_inserted_after_preview(tmp_path):
    directory, exported, service = exported_operation(tmp_path)
    plan = cleanup_plan(service, directory, exported)
    moved = tmp_path / "moved-workspace"
    (directory / "workspace").rename(moved)
    (directory / "workspace").symlink_to(moved)
    with pytest.raises(PreconditionError, match="scratch identity changed"):
        apply_cleanup(service, plan)
    assert (moved / "build/keep").is_file()
