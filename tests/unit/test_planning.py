import copy
import io
import os
import shutil
import subprocess
import tarfile

import pytest
import yaml

from pg_maker.api import MakerService
from pg_maker.assets import initialize_project
from pg_maker.config import environment_requirements, load_config, resource_path
from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.models import BuildRequest, parse_request
from pg_maker.planning.documents import seal, verify
from pg_maker.planning.plan import build_plan
from pg_maker.runners.docker import current_user
from pg_maker.sources.resolver import SourceResolver
from pg_maker.sources.store import InputStore, extract_archive, inventory
from pg_maker.storage import content_hash, file_hash, read_json, write_json
from pg_maker.yamlio import load_yaml


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False))


@pytest.fixture
def project(tmp_path):
    directory = tmp_path / "project"
    initialize_project(directory)
    # Synthetic sources intentionally have no contrib or external dependencies.
    feature = load_yaml(directory / "config/features/my-feature.yaml")
    feature["spec"]["components"] = []
    write_yaml(directory / "config/features/my-feature.yaml", feature)
    write_yaml(
        directory / "config/defaults.yaml",
        {"spec": {"execution": {"limits": {"min_free_disk_bytes": 1048576}}}},
    )
    source = directory / "source"
    source.mkdir()
    (source / "configure.ac").write_text("AC_INIT([PostgreSQL], [18.1], [bugs@example.invalid])\n")
    (source / "src/include").mkdir(parents=True)
    (source / "src/include/pg_config_manual.h").write_text('#define DEFAULT_PGSOCKET_DIR  "/tmp"\n')
    (source / "tool").write_text("#!/bin/sh\nexit 0\n")
    (source / "tool").chmod(0o755)
    (source / "link").symlink_to("tool")
    manifest = load_yaml(directory / "build.yaml")
    manifest["spec"]["source"] = {
        "kind": "snapshot",
        "path": "source",
        "include": ["**"],
        "exclude": [],
    }
    write_yaml(directory / "build.yaml", manifest)
    return directory


def environment(config):
    requirements = environment_requirements(config)
    images = [
        {
            "id": r["id"],
            "target": r["target"],
            "role": r["role"],
            "image_id": "sha256:" + "1" * 64,
            "base_image_id": "sha256:" + "2" * 64,
            "base_registry_digests": [],
            "registry_manifest_digest": None,
            "image_archive_sha256": None,
            "os": r["os"],
            "architecture": r["architecture"],
            "packages": [],
            "tools": {},
            "method": "apt-commit",
            "reproducibility": "unit fixture",
        }
        for r in requirements["environments"]
    ]
    return seal("environment", {"requirements_hash": content_hash(requirements), "images": images})


def resolved(project, *, offline=False, request_id=None):
    service = MakerService(project / "operations")
    config = load_config(project / "build.yaml")
    env = project / "env.json"
    write_json(env, environment(config.data))
    started = service.prepare(
        project / "build.yaml",
        output=project / "lock.json",
        input_store=project / "inputs",
        environments=env,
        offline=offline,
        request_id=request_id,
    )
    state = service.wait(service.directory / started["operation_id"], timeout_seconds=20)
    return state, read_json(project / "lock.json") if (project / "lock.json").is_file() else None


def test_previous_release_is_hashed_input_and_missing_is_not_a_skip(project):
    manifest = load_yaml(project / "build.yaml")
    packages = []
    for role in ("server", "client", "libpq"):
        path = project / (role + ".deb")
        path.write_bytes((role + " fixture bytes").encode())
        packages.append({"role": role, "path": path.name, "sha256": file_hash(path)})
    manifest["spec"]["previous_releases"] = [
        {"variant": "my-feature.debian13-x64", "postgresql_version": "18.0", "packages": packages}
    ]
    write_yaml(project / "build.yaml", manifest)
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state
    previous = lock["spec"]["config"]["previous_releases"][0]
    assert {p["sha256"] for p in previous["packages"]} <= set(lock["spec"]["blobs"].values())
    assert all("path" not in p for p in previous["packages"])
    (project / "server.deb").write_bytes(b"changed")
    state, _ = resolved(project)
    assert state["status"] == "failed"
    assert state["error"]["code"] == "precondition_failed"
    manifest["spec"]["previous_releases"][0]["postgresql_version"] = "17.7"
    write_yaml(project / "build.yaml", manifest)
    with pytest.raises(UnsupportedError, match="major-version"):
        load_config(project / "build.yaml")


def test_build_input_accepts_absent_optional_recovery_fields(project):
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    spec = copy.deepcopy(lock["spec"])
    del spec["config"]["previous_releases"]
    lock = seal("lock", spec, lock["transport"])
    plan = build_plan(lock, goal="source")
    document = BuildRequest(lock, plan, str(project / "inputs")).to_document()
    del document["cache_store"]
    request = parse_request(document)
    assert isinstance(request, BuildRequest)
    assert request.cache_store is None
    assert "previous_releases" not in request.lock["spec"]["config"]


@pytest.mark.parametrize(
    "text",
    [
        "x: 1\nx: 2\n",
        "x: {a: 1, a: 2}\n",
        "x: &x [a]\ny: *x\n",
        "x: !!python/object/apply:os.system ['echo bad']",
        "x: .nan",
        "x: 2020-01-01",
        "1: value",
    ],
)
def test_yaml_rejects_ambiguous_or_non_json_values(tmp_path, text):
    path = tmp_path / "bad.yaml"
    path.write_text(text)
    with pytest.raises(ValidationError):
        load_yaml(path)


def test_init_defaults_and_identity(project):
    result = load_config(project / "build.yaml")
    variant = result.data["variants"][0]
    assert variant["identity"]["packages"][2]["name"] == "test-my-feature-libpq5-18"
    assert variant["identity"]["product_version"] == "18.1.0"
    assert variant["socket_directory"] == "/var/run/postgresql"
    assert result.provenance["/features/0/metadata/id"].endswith("my-feature.yaml")
    assert result.provenance["/postgresql/version"].endswith("build.yaml")
    debug = load_config(project / "build.yaml", build_mode="debug").data["variants"][0]
    assert debug["identity"]["packages"][0]["name"].endswith("-debug")
    assert "-O0" in debug["build"]["resolved_flags"]["cflags"]
    assert not any("FORTIFY_SOURCE" in x for x in debug["build"]["resolved_flags"]["cppflags"])


def test_configuration_precedence_and_list_replacement(project, monkeypatch, tmp_path):
    env_config = tmp_path / "env_config"
    cli_config = tmp_path / "cli_config"
    for directory, brand in [(env_config, "environment"), (cli_config, "explicit")]:
        shutil.copytree(project / "config", directory)
        write_yaml(directory / "vendor.yaml", {"spec": {"brand": brand}})
    monkeypatch.setenv("PG_MAKER_CONFIG_DIR", str(env_config))
    assert load_config(project / "build.yaml").data["vendor"]["brand"] == "Test"
    assert (
        load_config(project / "build.yaml", config_dir=cli_config).data["vendor"]["brand"]
        == "explicit"
    )
    manifest = load_yaml(project / "build.yaml")
    del manifest["spec"]["config_dir"]
    write_yaml(project / "build.yaml", manifest)
    assert load_config(project / "build.yaml").data["vendor"]["brand"] == "environment"
    write_yaml(
        env_config / "defaults.yaml",
        {"spec": {"common_changes": [], "build": {"nls_languages": ["en"]}}},
    )
    result = load_config(project / "build.yaml").data
    assert result["variants"][0]["socket_directory"] == "/tmp"
    assert result["defaults"]["build"]["nls_languages"] == ["en"]


@pytest.mark.parametrize(
    "flag", ["--prefix=/tmp", "--enable-debug", "--with-llvm", "-O3", "-march=native", "CFLAGS=-O0"]
)
def test_managed_flags_cannot_be_overridden(project, flag):
    feature = load_yaml(project / "config/features/my-feature.yaml")
    feature["spec"]["build_options"] = {"configure_args": [flag]}
    write_yaml(project / "config/features/my-feature.yaml", feature)
    with pytest.raises(ValidationError, match="managed"):
        load_config(project / "build.yaml")


def test_unknown_fields_and_future_capabilities_are_explicit(project):
    write_yaml(project / "config/vendor.yaml", {"spec": {"brnad": "mistake"}})
    with pytest.raises(ValidationError) as error:
        load_config(project / "build.yaml")
    assert error.value.details["pointer"] == "/spec/brnad"
    (project / "config/vendor.yaml").unlink()
    assert load_config(project / "build.yaml", with_doc=True).data["documentation"]["enabled"]
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["targets"][0]["architecture"] = "aarch64"
    write_yaml(project / "build.yaml", manifest)
    with pytest.raises(UnsupportedError):
        load_config(project / "build.yaml")


def test_builtin_recipe_override_needs_explicit_intent(project):
    recipe = load_yaml(resource_path("recipes/platforms/debian-13.yaml"))
    write_yaml(project / "config/platforms/debian-13.yaml", recipe)
    with pytest.raises(ValidationError, match="override"):
        load_config(project / "build.yaml")
    recipe["spec"]["replace_builtin"] = True
    write_yaml(project / "config/platforms/debian-13.yaml", recipe)
    load_config(project / "build.yaml")


def test_feature_collisions_and_numeric_ids(project):
    feature = load_yaml(project / "config/features/my-feature.yaml")
    feature["metadata"]["id"] = "other"
    write_yaml(project / "config/features/other.yaml", feature)
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["features"].append("other")
    write_yaml(project / "build.yaml", manifest)
    assert len(load_config(project / "build.yaml").data["variants"]) == 2
    write_yaml(
        project / "config/defaults.yaml", {"spec": {"paths": {"data_directory": "/var/lib/shared"}}}
    )
    with pytest.raises(ValidationError, match="collision"):
        load_config(project / "build.yaml")


def test_snapshot_hash_preserves_modes_links_and_ignores_mtime(project, tmp_path):
    store = InputStore(tmp_path / "inputs")
    resolver = SourceResolver(store, tmp_path / "worker")
    source = {"kind": "snapshot", "path": "source", "include": ["**"], "exclude": ["ignored"]}
    first = resolver.resolve(source, project)
    (project / "source/ignored").write_text("ignored")
    os.utime(project / "source/tool", (1, 1))
    assert resolver.resolve(source, project) == first
    entries = inventory(store.tree(first["source_hash"]))["entries"]
    assert next(x for x in entries if x["path"] == "tool")["mode"] == 0o755
    assert next(x for x in entries if x["path"] == "link")["target"] == "tool"
    (project / "source/tool").chmod(0o644)
    assert resolver.resolve(source, project)["source_hash"] != first["source_hash"]


def test_documentation_aliases_dependencies_and_explicit_disable(project):
    config = load_config(project / "build.yaml", with_doc=True, doc_languages=["en-US", "ru-RU"])
    assert config.data["documentation"]["languages"] == ["en", "ru"]
    requirements = environment_requirements(config.data)
    packages = {p for item in requirements["environments"] for p in item["packages"]}
    assert {"python3-lxml", "docbook-xsl", "fonts-dejavu-core"} <= packages
    roles = {p["role"] for p in config.data["variants"][0]["identity"]["packages"]}
    assert "docs" in roles
    with pytest.raises(ValidationError):
        load_config(project / "build.yaml", with_doc=False, doc_languages=["ru"])
    with pytest.raises(ValidationError):
        load_config(project / "build.yaml", with_doc=True, doc_languages=["en", "en-US"])
    with pytest.raises(ValidationError):
        load_config(project / "build.yaml", with_doc=True, doc_languages=["unknown"])


def test_documentation_assets_are_pinned_and_docs_plan_never_runs_database_tests(project):
    profile_path = project / "config/documentation.yaml"
    profile = load_yaml(profile_path)
    fragment = project / "config/fragment.xml"
    fragment.write_text("<para>Local release notes</para>")
    profile["spec"].update(
        enabled=True,
        rules=[
            {
                "api_version": "pg_maker/doc-rules/v1",
                "kind": "DocumentationRules",
                "metadata": {"id": "custom", "revision": 1},
                "spec": {
                    "rules": [
                        {
                            "id": "addition",
                            "action": "append",
                            "file": "config.sgml",
                            "selector": {"id": "config"},
                            "expect_matches": 1,
                            "fragment": "fragment.xml",
                        }
                    ]
                },
            }
        ],
    )
    write_yaml(profile_path, profile)
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state
    assert file_hash(fragment) in lock["spec"]["blobs"].values()
    plan = build_plan(lock, goal="docs", run_db_tests=False)
    kinds = [s["kind"] for s in plan["spec"]["steps"]]
    assert "prepare_documentation" in kinds and "build_docs_en" in kinds
    assert "export_documentation" in kinds
    assert not any("compile" in k or "acceptance" in k or "package" in k for k in kinds)
    fragment.write_text("<para>Changed release notes</para>")
    state, changed = resolved(project)
    assert state["status"] == "succeeded", state
    assert changed["lock_hash"] != lock["lock_hash"]


@pytest.mark.parametrize("link", ["/etc/passwd", "../../escape"])
def test_snapshot_rejects_escape(project, tmp_path, link):
    (project / "source/evil").symlink_to(link)
    with pytest.raises(ValidationError, match="symlink"):
        SourceResolver(InputStore(tmp_path / "store"), tmp_path / "worker").resolve(
            {"kind": "snapshot", "path": "source", "include": ["**"], "exclude": []}, project
        )


@pytest.mark.parametrize(
    "kind,name,link",
    [
        (tarfile.REGTYPE, "../escape", ""),
        (tarfile.REGTYPE, "/absolute", ""),
        (tarfile.SYMTYPE, "link", "../../escape"),
        (tarfile.CHRTYPE, "dev", ""),
    ],
)
def test_archive_rejects_unsafe_entries(tmp_path, kind, name, link):
    path = tmp_path / "input.tar"
    with tarfile.open(path, "w") as archive:
        entry = tarfile.TarInfo(name)
        entry.type, entry.linkname = kind, link
        archive.addfile(entry, io.BytesIO(b""))
    destination = tmp_path / "tree"
    destination.mkdir()
    with pytest.raises(ValidationError):
        extract_archive(path, destination)


def git(path, *args):
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


def test_git_locks_committed_tree_not_dirty_files(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.email", "test@example.invalid")
    git(repository, "config", "user.name", "Test")
    (repository / "file").write_text("committed")
    git(repository, "add", "file")
    git(repository, "commit", "-m", "fixture")
    commit = git(repository, "rev-parse", "HEAD")
    (repository / "file").write_text("dirty")
    (repository / "untracked").write_text("untracked")
    store = InputStore(tmp_path / "store")
    resolver = SourceResolver(store, tmp_path / "worker")
    spec = {"kind": "git", "repository": str(repository), "ref": "HEAD"}
    result = resolver.resolve(spec, tmp_path)
    assert result["commit"] == commit
    tree = store.tree(result["source_hash"])
    assert (tree / "file").read_text() == "committed"
    assert not (tree / "untracked").exists()
    shutil.rmtree(repository)
    assert (
        SourceResolver(store, tmp_path / "offline", offline=True).resolve(spec, tmp_path) == result
    )


def test_archive_checksum_and_offline_copy(project, tmp_path):
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as out:
        out.add(project / "source", arcname="postgresql")
    spec = {
        "kind": "archive",
        "location": str(archive),
        "sha256": file_hash(archive),
        "strip_components": 1,
    }
    store = InputStore(tmp_path / "store")
    resolver = SourceResolver(store, tmp_path / "worker")
    with pytest.raises(PreconditionError, match="checksum"):
        resolver.resolve({**spec, "sha256": "sha256:" + "0" * 64}, tmp_path)
    first = resolver.resolve(spec, tmp_path)
    archive.unlink()
    assert (
        SourceResolver(store, tmp_path / "offline", offline=True).resolve(spec, tmp_path) == first
    )


def test_durable_resolve_offline_plan_and_tampering(project, tmp_path):
    state, lock = resolved(project, request_id="resolve-1")
    assert state["status"] == "succeeded", state["error"]
    validate_document(lock, "lock")
    before = {p: p.stat().st_mtime_ns for p in (project / "inputs").rglob("*")}
    plan = build_plan(lock)
    assert before == {p: p.stat().st_mtime_ns for p in (project / "inputs").rglob("*")}
    assert plan["spec"]["execution_available"] is True
    assert "compile_core" in [step["kind"] for step in plan["spec"]["steps"]]
    assert plan["plan_hash"] == build_plan(lock)["plan_hash"]
    assert plan["plan_hash"] != build_plan(lock, goal="source")["plan_hash"]
    copied = tmp_path / "copied"
    shutil.copytree(project / "inputs", copied, symlinks=True)
    assert build_plan(lock, input_store=copied)["plan_hash"] == plan["plan_hash"]
    lock2 = copy.deepcopy(lock)
    lock2["spec"]["config"]["vendor"]["brand"] = "changed"
    with pytest.raises(PreconditionError, match="hash"):
        build_plan(lock2)
    (project / "source/tool").write_text("changed after resolve")
    state2, offline = resolved(project, offline=True)
    assert state2["status"] == "succeeded", state2["error"]
    assert offline["lock_hash"] == lock["lock_hash"]
    tree = InputStore(project / "inputs").tree(lock["spec"]["config"]["source"]["source_hash"])
    (tree / "tool").write_text("corrupted")
    with pytest.raises(PreconditionError, match="changed"):
        build_plan(lock)


def test_stale_environment_rejected_before_operation(project):
    config = load_config(project / "build.yaml")
    write_json(project / "env.json", environment(config.data))
    write_yaml(project / "config/defaults.yaml", {"spec": {"build": {"options": {"llvm": False}}}})
    service = MakerService(project / "operations")
    with pytest.raises(PreconditionError, match="stale"):
        service.prepare(
            project / "build.yaml",
            output=project / "lock.json",
            input_store=project / "inputs",
            environments=project / "env.json",
        )
    assert not service.directory.exists()


def test_offline_missing_inputs_and_source_version_mismatch(project):
    state, lock = resolved(project, offline=True)
    assert state["status"] == "failed"
    assert state["error"]["details"]["missing"] == ["postgresql"]
    assert lock is None
    (project / "source/configure.ac").write_text("AC_INIT([PostgreSQL], [18.2], [bugs])")
    state, lock = resolved(project)
    assert state["status"] == "failed"
    assert "version differs" in state["error"]["message"]


def test_document_hash_excludes_transport_and_time():
    document = environment(load_config(resource_path("bundled/build.yaml")).data)
    document["transport"] = {"input_store": "/another/path"}
    document["created_at"] = "changed"
    verify(document, "environment")
    document["spec"]["images"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        verify(document, "environment")


def add_change(project, identifier, *, requires=(), socket=None, acceptance=False):
    directory = project / "changes" / identifier
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fix.patch").write_text("--- a/tool\n+++ b/tool\n@@ -2 +2 @@\n-exit 0\n+exit 1\n")
    spec = {
        "compatible_postgresql": ">=18.1,<19",
        "requires": list(requires),
        "conflicts": [],
        "operations": [{"id": "fix", "kind": "patch", "file": "fix.patch"}],
        "acceptance": [],
        "compatibility": {
            "data_format": "unchanged",
            "wal_format": "unchanged",
            "extension_abi": "unknown",
        },
    }
    if socket:
        spec["postconditions"] = {"socket_directory": socket}
    if acceptance:
        (directory / "test.sql").write_text("SELECT 1;\n")
        spec["acceptance"] = [
            {
                "id": "behavior",
                "requirement_ids": ["REQ-1"],
                "kind": "sql",
                "setup": {"cluster": "fresh", "database": "test"},
                "script": "test.sql",
                "assertions": [{"id": "value", "source": "sql", "equals": 1}],
                "timeout_seconds": 30,
                "expectations": {
                    "candidate": {"outcome": "pass"},
                    "baseline": {"outcome": "assertion-failure", "assertion_id": "value"},
                },
            }
        ]
    write_yaml(
        directory / "change.yaml",
        {
            "api_version": "pg_maker/change-set/v1",
            "kind": "ChangeSet",
            "metadata": {"id": identifier, "revision": 1},
            "spec": spec,
        },
    )
    return "../../changes/" + identifier + "/change.yaml"


def select_changes(project, references):
    path = project / "config/features/my-feature.yaml"
    feature = load_yaml(path)
    feature["spec"]["changes"] = references
    write_yaml(path, feature)


def test_change_order_baseline_and_frozen_files(project):
    first = add_change(project, "first", requires=["last"], acceptance=True)
    middle = add_change(project, "middle")
    last = add_change(project, "last")
    select_changes(project, [first, middle, last, first])
    config = load_config(project / "build.yaml")
    assert [c["metadata"]["id"] for c in config.data["features"][0]["spec"]["changes"]] == [
        "middle",
        "last",
        "first",
    ]
    assert config.data["variants"][0]["baseline"] is True
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state["error"]
    plan = build_plan(lock, goal="verified")
    assert {s["branch"] for s in plan["spec"]["steps"]} == {"candidate", "baseline"}
    baseline = next(
        s
        for s in plan["spec"]["steps"]
        if s["branch"] == "baseline" and s["kind"] == "apply_changes"
    )
    assert baseline["parameters"]["ordered_changes"] == ["builtin/unix-socket-directory"]
    (project / "changes/first/fix.patch").write_text("changed")
    assert build_plan(lock, goal="verified")["plan_hash"] == plan["plan_hash"]
    state2, lock2 = resolved(project)
    assert state2["status"] == "succeeded", state2["error"]
    assert lock2["lock_hash"] != lock["lock_hash"]


def test_change_cycles_socket_conflicts_and_baseline_requirement(project):
    first = add_change(project, "first", requires=["second"])
    second = add_change(project, "second", requires=["first"])
    select_changes(project, [first, second])
    with pytest.raises(ValidationError, match="cycle"):
        load_config(project / "build.yaml")
    first = add_change(project, "first", socket="/other/socket", acceptance=True)
    select_changes(project, [first])
    with pytest.raises(ValidationError, match="socket_directory"):
        load_config(project / "build.yaml")
    write_yaml(
        project / "config/defaults.yaml",
        {"spec": {"common_changes": [], "validation": {"baseline": False}}},
    )
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["validation"]["baseline"] = False
    write_yaml(project / "build.yaml", manifest)
    with pytest.raises(ValidationError, match="baseline"):
        load_config(project / "build.yaml")


def test_git_submodules_are_frozen_recursively(tmp_path):
    child = tmp_path / "child"
    parent = tmp_path / "parent"
    for repo in (child, parent):
        repo.mkdir()
        git(repo, "init")
        git(repo, "config", "user.email", "test@example.invalid")
        git(repo, "config", "user.name", "Test")
        (repo / "file").write_text(repo.name)
        git(repo, "add", "file")
        git(repo, "commit", "-m", "fixture")
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "modules/child")
    git(parent, "commit", "-am", "submodule")
    store = InputStore(tmp_path / "store")
    result = SourceResolver(store, tmp_path / "worker").resolve(
        {"kind": "git", "repository": str(parent), "ref": "HEAD"}, tmp_path
    )
    assert result["submodules"][0]["commit"] == git(child, "rev-parse", "HEAD")
    assert (store.tree(result["source_hash"]) / "modules/child/file").read_text() == "child"


def test_component_dependency_order_and_inputs(project):
    component = {
        "api_version": "pg_maker/component/v1",
        "kind": "ComponentRecipe",
        "metadata": {"id": "custom", "revision": 1},
        "spec": {
            "type": "pgxs",
            "source": {"kind": "snapshot", "path": "../source", "include": ["**"], "exclude": []},
            "compatible_postgresql": ">=18.1,<19",
            "requires": [],
            "conflicts": [],
            "build_dependencies": ["libxml2-dev"],
            "runtime_dependencies": [],
            "make_targets": ["all"],
            "test_targets": ["installcheck"],
            "install_targets": ["install"],
            "required_tests": ["custom-test"],
            "runtime_files": ["lib/custom.so"],
        },
    }
    write_yaml(project / "components/custom.yaml", component)
    path = project / "config/features/my-feature.yaml"
    feature = load_yaml(path)
    feature["spec"]["components"] = ["../../components/custom.yaml"]
    write_yaml(path, feature)
    config = load_config(project / "build.yaml")
    assert "libxml2-dev" in environment_requirements(config.data)["environments"][0]["packages"]
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state["error"]
    pinned = lock["spec"]["config"]["features"][0]["spec"]["components"][0]["spec"]["source"]
    assert "source_hash" in pinned and "path" not in pinned


def test_environment_checks_pinned_dependency_versions_and_architecture():
    from pg_maker.planning.environments import missing_packages

    inventory = [{"name": "libpq5", "version": "18.1-1", "architecture": "amd64"}]
    assert missing_packages(["libpq5", "libpq5:amd64=18.1-1"], inventory) == []
    assert missing_packages(["libpq5=18.2-1", "libpq5:arm64", "absent"], inventory) == [
        "libpq5=18.2-1",
        "libpq5:arm64",
        "absent",
    ]


def test_source_build_operation_rejects_modified_plan_and_is_idempotent(project):
    write_yaml(
        project / "config/defaults.yaml",
        {
            "spec": {
                "common_changes": [],
                "build": {"jobs": 1},
                "execution": {"build_user": current_user(), "limits": {"min_free_disk_bytes": 1}},
            }
        },
    )
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    plan = build_plan(lock, goal="source")
    assert plan["spec"]["execution_available"]
    service = MakerService(project / "operations")
    altered = copy.deepcopy(plan)
    altered["spec"]["steps"].pop()
    altered = seal("plan", altered["spec"], altered["transport"])
    write_json(project / "altered.json", altered)
    with pytest.raises(PreconditionError, match="differs"):
        service.build(project / "lock.json", goal="source", plan=project / "altered.json")
    started = service.build(project / "lock.json", goal="source", request_id="source-once")
    completed = service.wait(service.directory / started["operation_id"], timeout_seconds=30)
    assert completed["status"] == "succeeded", completed
    assert completed["result"]["qualification"] == "unverified"
    assert not completed["resources"]
    repeated = service.build(project / "lock.json", goal="source", request_id="source-once")
    assert repeated["operation_id"] == started["operation_id"]
    assert repeated["status"] == "succeeded"
    source = (
        service.directory
        / started["operation_id"]
        / "workspace/build/my-feature.debian13-x64.candidate/source"
    )
    assert (source / "tool").read_bytes() == (project / "source/tool").read_bytes()


def test_build_graph_requires_tap_and_tests_on_both_branches(project):
    select_changes(project, [add_change(project, "behavior-change", acceptance=True)])
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    plan = build_plan(lock, goal="build", run_db_tests=True)
    assert plan["spec"]["run_db_tests"] is True
    assert {s["branch"] for s in plan["spec"]["steps"]} == {"candidate", "baseline"}
    kinds = [s["kind"] for s in plan["spec"]["steps"]]
    for kind in ("expected_test_inventory", "test_core", "test_source_world", "export_build"):
        assert kind in kinds
    configure = next(s for s in plan["spec"]["steps"] if s["kind"] == "configure")
    assert "--enable-tap-tests" in configure["argv"]
    assert "ssl libpq_encryption" == configure["env"]["PG_TEST_EXTRA"]
    package = build_plan(lock, goal="package")
    assert {"collect_install_tree", "runtime_dependencies", "package", "export"} <= {
        s["kind"] for s in package["spec"]["steps"]
    }


def test_fast_build_skips_db_tests_and_baseline_but_keeps_build_steps(project):
    select_changes(project, [add_change(project, "behavior-change", acceptance=True)])
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    assert lock["spec"]["config"]["variants"][0]["baseline"] is True
    plan = build_plan(lock, goal="build")
    assert plan["spec"]["run_db_tests"] is False
    assert {s["branch"] for s in plan["spec"]["steps"]} == {"candidate"}
    kinds = {s["kind"] for s in plan["spec"]["steps"]}
    assert {
        "compile_core",
        "install_build_prefix",
        "compile_components",
        "install_components",
        "export_build",
    } <= kinds
    assert (
        not {
            "expected_test_inventory",
            "test_core",
            "test_source_world",
            "build_and_test_tools",
            "acceptance_build_prefix",
        }
        & kinds
    )
    full = build_plan(lock, goal="build", run_db_tests=True)
    assert full["plan_hash"] != plan["plan_hash"]
    assert build_plan(lock)["spec"]["run_db_tests"] is False
    assert not any(
        s["kind"] == "test_source_world" and s["branch"] == "baseline"
        for s in full["spec"]["steps"]
    )
    with pytest.raises(ValidationError, match="requires DB tests"):
        build_plan(lock, goal="verified", run_db_tests=False)


@pytest.mark.parametrize("goal", ["build", "package"])
def test_build_preserves_supplied_test_mode_and_rejects_conflicting_override(
    project, monkeypatch, goal
):
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    plan = build_plan(lock, goal=goal, run_db_tests=True)
    write_json(project / "plan.json", plan)
    service = MakerService(project / "operations")
    requests = []
    monkeypatch.setattr(service, "start_operation", lambda request, **kw: requests.append(request))
    service.build(project / "lock.json", plan=project / "plan.json")
    assert requests[0].plan["plan_hash"] == plan["plan_hash"]
    assert requests[0].plan["spec"]["run_db_tests"] is True
    assert requests[0].plan["spec"]["goal"] == goal
    with pytest.raises(PreconditionError, match="differs"):
        service.build(project / "lock.json", plan=project / "plan.json", run_db_tests=False)
    with pytest.raises(PreconditionError, match="plan hash"):
        service.build(project / "lock.json", plan_hash=plan["plan_hash"])
    assert len(requests) == 1


def test_db_tests_can_be_enabled_in_config_and_explicitly_disabled(project):
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["validation"]["run_db_tests"] = True
    write_yaml(project / "build.yaml", manifest)
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    assert build_plan(lock, goal="build")["spec"]["run_db_tests"] is True
    assert build_plan(lock, goal="build", run_db_tests=False)["spec"]["run_db_tests"] is False


def test_agent_workspace_uses_applied_change_prefix(project):
    from pg_maker.changes.apply import apply_patch
    from pg_maker.changes.workspace import export_changes, workspace

    defaults = load_yaml(project / "config/defaults.yaml")
    defaults["spec"]["common_changes"] = []
    write_yaml(project / "config/defaults.yaml", defaults)
    (project / "source/value").write_text("upstream\n")
    first, second = add_change(project, "first"), add_change(project, "second", requires=["first"])
    select_changes(project, [first, second])
    for name, old, new in (("first", "upstream", "first"), ("second", "first", "second")):
        (project / f"changes/{name}/fix.patch").write_text(
            f"--- a/value\n+++ b/value\n@@ -1 +1 @@\n-{old}\n+{new}\n"
        )
    state, lock = resolved(project)
    assert state["status"] == "succeeded"
    digest = lock["spec"]["config"]["source"]["source_hash"]
    work = project / "agent"
    record = workspace(project / "inputs", digest, work, lock=project / "lock.json")
    assert record["basis"] == "changes-prefix"
    assert (work / "source/value").read_text() == "second\n"
    (work / "source/value").write_text("third\n")
    export_changes(work, project / "export-change", "third", "==18.1")
    replay = project / "replay"
    shutil.copytree(InputStore(project / "inputs").tree(record["source_hash"]), replay)
    apply_patch(replay, (project / "export-change/changes.patch").read_text())
    assert (replay / "value").read_text() == "third\n"
    workspace(
        project / "inputs",
        digest,
        project / "before-second",
        lock=project / "lock.json",
        until="second/fix",
    )
    assert (project / "before-second/source/value").read_text() == "first\n"
    assert not list(work.glob("source.before-*"))
    assert InputStore(project / "inputs").tree(digest).joinpath("value").read_text() == "upstream\n"


def test_candidate_only_acceptance_does_not_force_baseline(project):
    change = add_change(project, "only-candidate", acceptance=True)
    path = project / "changes/only-candidate/change.yaml"
    document = load_yaml(path)
    del document["spec"]["acceptance"][0]["expectations"]["baseline"]
    write_yaml(path, document)
    select_changes(project, [change])
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["validation"]["baseline"] = False
    write_yaml(project / "build.yaml", manifest)
    assert load_config(project / "build.yaml").data["variants"][0]["baseline"] is False


def test_changes_validate_checks_acceptance_files(project):
    from pg_maker.changes.workspace import validate_changes

    add_change(project, "acceptance", acceptance=True)
    path = project / "changes/acceptance/change.yaml"
    assert validate_changes(path)["acceptance"]
    (path.parent / "test.sql").unlink()
    with pytest.raises(PreconditionError, match="acceptance script"):
        validate_changes(path)
