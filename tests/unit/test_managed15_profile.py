"""PG15 inventory coverage, honest compatibility claims and mandatory test policy."""

import pytest

from pg_maker.assets import initialize_project
from pg_maker.builders.autotools import validate_build
from pg_maker.config import environment_requirements, load_config
from pg_maker.packaging.contrib import REQUIRED_OPTIONS
from pg_maker.validation.upstream.pglogical import configure_tests
from pg_maker.yamlio import load_yaml


@pytest.mark.parametrize("version", ["15.18", "15.19"])
def test_pg15_profiles_preserve_contrib_and_managed_inventory(tmp_path, version):
    initialize_project(tmp_path, profile=f"yc-{version}-icu70")
    config = load_config(tmp_path / "build.yaml").data
    validate_build(config, "verified")
    assert config["postgresql"]["version"] == version
    assert config["postgresql_recipe"]["db_test_adapter"] == "pg15"
    assert config["defaults"]["validation"]["run_db_tests"] is True
    assert config["defaults"]["packaging"]["standard_contrib"] is True
    variant = config["variants"][0]
    assert all(variant["build"]["options"][name] for name in REQUIRED_OPTIONS)
    assert variant["identity"]["paths"]["runtime_prefix"] == "/opt/postgresql-yc-clone/15"
    assert {p["filename"] for p in variant["identity"]["packages"]} == {
        f"postgresql-15-yc-clone_{version}-1.deb13_amd64.deb",
        f"postgresql-client-15-yc-clone_{version}-1.deb13_amd64.deb",
        f"libpq5-yc-clone_{version}-1.deb13_amd64.deb",
    }
    components = {
        c["metadata"]["id"]: c["spec"] for c in config["features"][0]["spec"]["components"]
    }
    declared = {
        n: v for c in components.values() for n, v in c.get("sql_extension_versions", {}).items()
    }
    catalog = load_yaml(tmp_path / "catalogs/yc-15.19-extensions.yaml")
    entries = catalog["extensions"]
    assert len(entries) == len({e["name"] for e in entries}) == 36
    assert len(declared) == 32
    assert {e["name"] for e in entries if e["status"] == "blocked"} == {
        "anon",
        "oracle_fdw",
        "pgvector",
    }
    assert "yc15-plv8" not in components
    assert next(e for e in entries if e["name"] == "plv8")["status"] == "disabled"
    for e in entries:
        if e["status"] in {"blocked", "disabled"}:
            assert e["reason"] and e["name"] not in declared
        else:
            assert declared[e["name"]] == e["managed_default_version"]
            assert components[e["component_id"]]["source"]["sha256"] == e["source_sha256"]
    assert catalog["binary_compatibility"]["required"] is True
    assert catalog["binary_compatibility"]["status"] == "not-verified"
    assert {
        e["name"] for e in catalog["binary_compatibility"]["standard_extension_version_differences"]
    } == {"amcheck", "pageinspect"}
    assert components["icu70"]["pkg_config_versions"] == {"icu-uc": "70.1", "icu-i18n": "70.1"}
    assert "-DAPACHE_ONLY=ON" in components["yc15-timescaledb"]["configure_args"]
    packages = {p for e in environment_requirements(config)["environments"] for p in e["packages"]}
    assert "libicu-dev" not in packages


@pytest.mark.parametrize("minor,expected", [(18, False), (19, True)])
def test_pglogical_test_config_handles_output_plugin_guc_boundary(tmp_path, minor, expected):
    (tmp_path / "t").mkdir()
    text = "shared_preload_libraries = 'pglogical'\noutput_plugin_libraries = 'pglogical_output'\n"
    for name in ("regress-postgresql.conf", "t/perl-95-postgresql.conf"):
        (tmp_path / name).write_text(text)
    configure_tests(tmp_path, f"PostgreSQL 15.{minor}")
    for name in ("regress-postgresql.conf", "t/perl-95-postgresql.conf"):
        actual = (tmp_path / name).read_text()
        assert ("output_plugin_libraries" in actual) == expected
        assert "shared_preload_libraries = 'pglogical'" in actual
    with pytest.raises(ValueError, match="requires PostgreSQL 15"):
        configure_tests(tmp_path, "PostgreSQL 16.15")
