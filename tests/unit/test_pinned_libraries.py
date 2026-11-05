from types import SimpleNamespace

import pytest

from pg_maker.assets import initialize_project
from pg_maker.builders.components import verify_extension_versions
from pg_maker.config import environment_requirements, load_config
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.execution.build import BuildBranch
from pg_maker.planning.plan import build_plan
from pg_maker.planning.reuse import compilation_inputs
from pg_maker.yamlio import load_yaml
from tests.unit.test_planning import project as project_fixture
from tests.unit.test_planning import resolved, write_yaml

project = project_fixture


@pytest.mark.parametrize(
    "style,release,expected",
    [
        ("postgresql", 0, "16.15"),
        ("three-part", 0, "16.15.0"),
        ("three-part", 7, "16.15.7"),
    ],
)
def test_product_version_and_package_revision_are_independent(tmp_path, style, release, expected):
    initialize_project(tmp_path)
    path = tmp_path / "build.yaml"
    manifest = load_yaml(path)
    manifest["spec"]["postgresql"]["version"] = "16.15"
    manifest["spec"]["product"] = {
        "release": release,
        "package_revision": 3,
        "version_format": style,
    }
    write_yaml(path, manifest)
    identity = load_config(path).data["variants"][0]["identity"]
    assert identity["product_version"] == expected
    assert all(p["deb_version"] == expected + "-3.deb13" for p in identity["packages"])


def test_upstream_version_does_not_silently_discard_product_release(tmp_path):
    initialize_project(tmp_path, profile="yc-16.15-icu70")
    manifest = load_yaml(tmp_path / "build.yaml")
    manifest["spec"]["product"]["release"] = 2
    write_yaml(tmp_path / "build.yaml", manifest)
    with pytest.raises(ValidationError, match="release=0"):
        load_config(tmp_path / "build.yaml")


def test_managed_profile_preserves_all_38_names_and_explains_exclusions(tmp_path):
    initialize_project(tmp_path, profile="yc-16.15-icu70")
    config = load_config(tmp_path / "build.yaml").data
    assert config["defaults"]["packaging"]["server_dev_files"] is True
    assert config["variants"][0]["identity"]["product_version"] == "16.15"
    assert {p["role"]: p["name"] for p in config["variants"][0]["identity"]["packages"]} == {
        "server": "postgresql-16-yc-clone",
        "client": "postgresql-client-16-yc-clone",
        "libpq": "libpq5-yc-clone",
    }
    components = config["features"][0]["spec"]["components"]
    declared = {
        name: version
        for c in components
        for name, version in c["spec"].get("sql_extension_versions", {}).items()
    }
    entries = load_yaml(tmp_path / "catalogs/yc-16.15-extensions.yaml")["extensions"]
    assert len(entries) == len({e["name"] for e in entries}) == 38
    assert len(declared) == 33
    for entry in entries:
        if entry["status"] in {"blocked", "disabled"}:
            assert entry["reason"] and entry["name"] not in declared
        else:
            assert declared[entry["name"]] == entry["managed_default_version"]
    assert next(e for e in entries if e["name"] == "plv8")["status"] == "disabled"
    requirements = environment_requirements(config)
    test_image = next(r for r in requirements["environments"] if r["role"] == "test")
    assert "build-essential" in test_image["packages"]
    packages = {
        p for r in requirements["environments"] for p in r["packages"] + r["runtime_packages"]
    }
    assert "libicu-dev" not in packages
    assert "libicu76" not in packages
    icu = next(c for c in components if c["metadata"]["id"] == "icu70")
    assert icu["spec"]["pkg_config_versions"] == {"icu-i18n": "70.1", "icu-uc": "70.1"}


def library_fixture(project):
    path = project / "components/libraries/icu70.yaml"
    component = load_yaml(path)
    source = project / "tiny-library"
    source.mkdir()
    (source / "configure").write_text("#!/bin/sh\nexit 0\n")
    component["spec"]["source"] = {
        "kind": "snapshot",
        "path": "../../tiny-library",
        "include": ["**"],
        "exclude": [],
    }
    component["spec"].pop("source_directory")
    # This synthetic library does not contain ICU's Python test sources.
    component["spec"].pop("source_patches", None)
    write_yaml(path, component)
    feature_path = project / "config/features/my-feature.yaml"
    feature = load_yaml(feature_path)
    feature["spec"]["components"] = ["../../components/libraries/icu70.yaml"]
    write_yaml(feature_path, feature)
    return path


def test_library_runs_before_configure_and_changes_invalidate_core(project):
    path = library_fixture(project)
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state
    plan = build_plan(lock)
    kinds = [s["kind"] for s in plan["spec"]["steps"]]
    assert kinds.index("build_libraries") < kinds.index("configure")
    assert "build_tools" not in kinds
    configure = next(s for s in plan["spec"]["steps"] if s["kind"] == "configure")
    prefix = lock["spec"]["config"]["variants"][0]["identity"]["paths"]["runtime_prefix"]
    assert configure["env"]["PKG_CONFIG_PATH"] == prefix + "/lib/pkgconfig"
    assert configure["env"]["LD_LIBRARY_PATH"] == prefix + "/lib"
    assert "-Wl,-rpath," + prefix + "/lib" in configure["env"]["LDFLAGS"]
    before = next(s["stage_key"] for s in plan["spec"]["steps"] if s["kind"] == "compile_core")
    doc = load_yaml(path)
    doc["spec"]["pkg_config_versions"]["icu-uc"] = "71.1"
    write_yaml(path, doc)
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state
    after = next(
        s["stage_key"] for s in build_plan(lock)["spec"]["steps"] if s["kind"] == "compile_core"
    )
    assert before != after


def test_early_library_cannot_depend_on_postgresql_extension(project):
    path = library_fixture(project)
    doc = load_yaml(path)
    doc["spec"]["requires"] = ["pg-stat-statements"]
    write_yaml(path, doc)
    feature_path = project / "config/features/my-feature.yaml"
    feature = load_yaml(feature_path)
    feature["spec"]["components"].append("../../components/pg-stat-statements.yaml")
    write_yaml(feature_path, feature)
    with pytest.raises(ValidationError, match="cannot depend"):
        load_config(project / "build.yaml")


def test_control_version_mismatch_cannot_pass_a_quick_build(tmp_path):
    control = tmp_path / "prefix/share/postgresql/extension/vector.control"
    control.parent.mkdir(parents=True)
    branch = SimpleNamespace(root=tmp_path)
    spec = {"sql_extension_versions": {"vector": "0.8.2"}}
    with pytest.raises(PreconditionError, match="missing"):
        verify_extension_versions(branch, spec)
    control.write_text("default_version = '0.8.1'\n")
    with pytest.raises(PreconditionError, match="differs"):
        verify_extension_versions(branch, spec)
    control.write_text("default_version = '0.8.2'\n")
    verify_extension_versions(branch, spec)


@pytest.mark.parametrize("actual,libdir", [("76.1", "/opt/test/lib"), ("70.1", "/usr/lib")])
def test_library_rejects_wrong_version_or_system_prefix(tmp_path, monkeypatch, actual, libdir):
    monkeypatch.setattr("pg_maker.builders.components.build_components", lambda *a, **kw: None)
    count = 0

    def command(argv):
        nonlocal count
        count += 1
        output = tmp_path / str(count)
        output.write_text(actual if argv[1] == "--modversion" else libdir)
        return SimpleNamespace(stdout_log=str(output))

    branch = SimpleNamespace(
        context=SimpleNamespace(check=lambda: None),
        request=SimpleNamespace(plan={"spec": {"run_db_tests": True}}),
        prepare=lambda: None,
        prefix="/opt/test",
        command=command,
    )
    step = {
        "kind": "build_libraries",
        "parameters": {
            "components": [{"spec": {"pkg_config_versions": {"icu-uc": "70.1"}}}],
        },
    }
    with pytest.raises(PreconditionError, match="version or prefix"):
        BuildBranch.execute(branch, step)


def test_auxiliary_sources_are_frozen_and_part_of_compilation_identity(project):
    path = library_fixture(project)
    document = load_yaml(path)
    extra = project / "auxiliary"
    extra.mkdir()
    (extra / "input.c").write_text("int value = 1;\n")
    document["spec"]["extra_sources"] = {
        "fixture": {
            "kind": "snapshot",
            "path": "../../auxiliary",
            "include": ["**"],
            "exclude": [],
        },
    }
    write_yaml(path, document)
    state, locked = resolved(project)
    assert state["status"] == "succeeded", state
    config = locked["spec"]["config"]
    feature = config["features"][0]
    source = feature["spec"]["components"][0]["spec"]["extra_sources"]["fixture"]
    assert source["source_hash"].startswith("sha256:")
    before, _ = compilation_inputs(locked, feature, config["variants"][0])
    (extra / "input.c").write_text("int value = 2;\n")
    # The existing lock can still plan with the frozen bytes.
    build_plan(locked)
    state, newer = resolved(project)
    assert state["status"] == "succeeded", state
    config = newer["spec"]["config"]
    after, _ = compilation_inputs(newer, config["features"][0], config["variants"][0])
    assert before != after
