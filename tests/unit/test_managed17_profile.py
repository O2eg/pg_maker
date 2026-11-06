import pytest

from pg_maker.assets import initialize_project
from pg_maker.config import environment_requirements, load_config
from pg_maker.validation.upstream import vectorscale
from pg_maker.yamlio import load_yaml


def test_pg17_inventory_private_icu_and_package_identity(tmp_path):
    initialize_project(tmp_path, profile="yc-17.11-icu70")
    config = load_config(tmp_path / "build.yaml").data
    assert config["postgresql"]["version"] == "17.11"
    assert config["postgresql_recipe"]["db_test_adapter"] == "pg17"
    assert config["defaults"]["validation"]["run_db_tests"] is True
    components = {
        c["metadata"]["id"]: c["spec"] for c in config["features"][0]["spec"]["components"]
    }
    declared = {
        n: v for c in components.values() for n, v in c.get("sql_extension_versions", {}).items()
    }
    catalog = load_yaml(tmp_path / "catalogs/yc-17.11-extensions.yaml")
    entries = catalog["extensions"]
    assert len(entries) == len({e["name"] for e in entries}) == 44
    assert len(declared) == 39
    for entry in entries:
        if entry["status"] in {"blocked", "disabled"}:
            assert entry["reason"] and entry["name"] not in declared
        else:
            assert declared[entry["name"]] == entry["managed_default_version"]
            recipe = load_yaml(tmp_path / "catalogs" / entry["recipe"])
            source = recipe["spec"]["source"]
            if source["kind"] == "git":
                assert source["ref"] == entry["source_ref"]
            elif "codeload.github.com" in source["location"]:
                assert source["location"].endswith("/" + entry["source_ref"])
    assert "yc17-plv8" not in components
    assert "yc17-repl-mon" not in components
    assert "repl_mon" in catalog["unobserved_preload_modules"]
    assert declared["age"] == "1.5.0" and declared["orafce"] == "4.15"
    assert components["icu70"]["pkg_config_versions"] == {"icu-uc": "70.1", "icu-i18n": "70.1"}
    assert "153.112.40" in str(components["icu70"]["installed_checks"])
    requirements = environment_requirements(config)
    packages = {
        p for e in requirements["environments"] for p in e["packages"] + e["runtime_packages"]
    }
    assert not {"libicu-dev", "libicu76"} & packages
    rust = components["yc17-vectorscale"]
    assert len(rust["extra_sources"]) == 626
    assert {p["filename"] for p in config["variants"][0]["identity"]["packages"]} == {
        "postgresql-17-yc-clone_17.11-1.deb13_amd64.deb",
        "postgresql-client-17-yc-clone_17.11-1.deb13_amd64.deb",
        "libpq5-yc-clone_17.11-1.deb13_amd64.deb",
    }


@pytest.mark.parametrize("major", [17, 18])
def test_vectorscale_build_uses_installed_pg_config(tmp_path, monkeypatch, major):
    def version(argv, *, text):
        assert argv == ["/private/bin/pg_config", "--version"] and text
        return f"PostgreSQL {major}.11\n"

    monkeypatch.setattr(vectorscale.subprocess, "check_output", version)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        vectorscale.sys, "argv", ["helper", "build", "/private", "8", "/inputs/cargo-pgrx"]
    )
    monkeypatch.setattr(vectorscale, "prepare", lambda *args: None)
    commands = []
    monkeypatch.setattr(vectorscale, "run", lambda argv, **kwargs: commands.append(argv))
    vectorscale.main()
    assert ["cargo", "pgrx", "init", f"--pg{major}=/private/bin/pg_config"] in commands
    install = next(c for c in commands if c[:3] == ["cargo", "pgrx", "install"])
    assert install[install.index("--features") + 1] == f"pg{major},build_parallel"


def test_vectorscale_rejects_unsupported_major(monkeypatch):
    monkeypatch.setattr(
        vectorscale.subprocess, "check_output", lambda *a, **kw: "PostgreSQL 16.15\n"
    )
    with pytest.raises(ValueError, match="requires PostgreSQL 17 or 18"):
        vectorscale.postgresql_feature("/private")


def test_vectorscale_ignored_upgrades_are_not_claimed_as_passed():
    ignored = [
        "access_method::upgrade_test::tests::test_upgrade_from_" + v
        for v in (
            "0_0_2",
            "0_2_0",
            "0_3_0",
            "0_4_0",
            "0_5_0",
            "0_5_1",
            "0_6_0",
            "0_7_0",
            "0_7_1",
            "0_8_0",
        )
    ]
    report = vectorscale.upgrade_test_report("pg17", ignored)
    assert report["status"] == "upstream-disabled; not executed or counted as passed"
    assert "no PostgreSQL 17 build" not in report["reason"]
    assert vectorscale.upgrade_test_report("pg18", ignored)["status"].startswith("not-applicable")
    for changed in (ignored[:-1], ignored + ["unexpected"], ignored + [ignored[0]]):
        with pytest.raises(RuntimeError, match="unexpected pgrx"):
            vectorscale.upgrade_test_report("pg17", changed)
