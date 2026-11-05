from pathlib import Path

import pytest

from pg_maker.assets import initialize_project
from pg_maker.config import load_config
from pg_maker.errors import ValidationError
from pg_maker.planning.reuse import compilation_recipe
from pg_maker.yamlio import load_yaml


def test_pg18_profile_inventory_versions_and_offline_rust_inputs(tmp_path):
    initialize_project(tmp_path, profile="yc-18.6-icu70")
    config = load_config(tmp_path / "build.yaml").data
    assert config["postgresql"]["version"] == "18.6"
    assert config["defaults"]["validation"]["run_db_tests"] is True
    assert config["postgresql_recipe"]["db_test_adapter"] == "pg18"
    components = {c["metadata"]["id"]: c for c in config["features"][0]["spec"]["components"]}
    declared = {
        n: v
        for c in components.values()
        for n, v in c["spec"].get("sql_extension_versions", {}).items()
    }
    entries = load_yaml(tmp_path / "catalogs/yc-18.6-extensions.yaml")["extensions"]
    assert len(entries) == len({e["name"] for e in entries}) == 44
    assert len(declared) == 39
    for entry in entries:
        if entry["status"] in {"blocked", "disabled"}:
            assert entry["reason"] and entry["name"] not in declared
        else:
            assert declared[entry["name"]] == entry["managed_default_version"]
    assert "yc18-plv8" not in components
    assert components["yc18-repl-mon"]["spec"]["preload"] == ["repl_mon"]
    assert components["icu70"]["spec"]["pkg_config_versions"]["icu-uc"] == "70.1"
    rust = components["yc18-vectorscale"]["spec"]
    assert "extra_source_catalogs" not in rust
    assert len([k for k in rust["extra_sources"] if k.startswith("crate-")]) == 621
    assert all(s["sha256"].startswith("sha256:") for s in rust["extra_sources"].values())
    assert not rust["make_targets"] and rust["build_commands"]
    assert {p["filename"] for p in config["variants"][0]["identity"]["packages"]} == {
        "postgresql-18-yc-clone_18.6-1.deb13_amd64.deb",
        "postgresql-client-18-yc-clone_18.6-1.deb13_amd64.deb",
        "libpq5-yc-clone_18.6-1.deb13_amd64.deb",
    }


def test_catalog_cannot_silently_replace_an_extra_source(tmp_path):
    initialize_project(tmp_path, profile="yc-18.6-icu70")
    catalog = tmp_path / "catalogs/yc18-cargo-sources.yaml"
    catalog.write_text(catalog.read_text() + "rustc:\n  kind: archive\n")
    with pytest.raises(ValidationError, match="duplicate extra source"):
        load_config(tmp_path / "build.yaml")


def test_explicit_build_command_changes_invalidate_component_reuse():
    component = {
        "metadata": {"id": "rust"},
        "spec": {
            "type": "utility",
            "build_commands": [{"argv": ["python3", "build.py", "--release"]}],
        },
    }
    old = compilation_recipe(component)
    component["spec"]["build_commands"] = [{"argv": ["python3", "build.py", "--debug"]}]
    assert compilation_recipe(component) != old


def test_pg18_helpers_are_installed_python_resources():
    from pg_maker.config import resource_path

    for name in ("vectorscale", "lwaldump", "partman5"):
        assert Path(resource_path("validation", "upstream", name + ".py")).is_file()
