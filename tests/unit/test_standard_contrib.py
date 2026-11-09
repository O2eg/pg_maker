"""Complete contrib policy and independent missing-file detection."""

import subprocess
from types import SimpleNamespace

import pytest
import yaml

from pg_maker.assets import BUILD_PROFILES, initialize_project
from pg_maker.builders.autotools import skip_policy, verify_configure
from pg_maker.config import load_config
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.packaging.contrib import REQUIRED_OPTIONS, capture, selected_files
from pg_maker.sources.store import inventory
from pg_maker.storage import read_json, write_json
from pg_maker.validation.installed import Harness


def test_empty_make_settings_do_not_consume_the_next_option():
    settings = "enable_tap_tests = yes\nwith_system_tzdata = \nwith_uuid = e2fs\n"
    assert verify_configure(["configure", "--with-uuid=e2fs"], settings, "", "")["passed"]
    with pytest.raises(PreconditionError, match="configured options differ"):
        verify_configure(["configure", "--without-uuid"], settings, "", "")


@pytest.mark.parametrize("extra,source_matches", [("", True), ("sepgsql", True), ("", False)])
def test_sepgsql_upstream_opt_in_is_explicit_not_a_blanket_skip(extra, source_matches):
    reason = "Potentially unsafe test sepgsql not enabled in PG_TEST_EXTRA"
    source = "plan skip_all => '" + reason + "';" if source_matches else ""
    observed = skip_policy("contrib/sepgsql/t/001_sepgsql.pl", source, {"PG_TEST_EXTRA": extra})
    assert observed == (reason if not extra and source_matches else None)


def test_contrib_inventory_uses_vpath_makefile_and_an_empty_destdir(tmp_path):
    source = tmp_path / "source/contrib"
    (source / "auto_explain").mkdir(parents=True)
    (source / "auto_explain/Makefile").write_text("# installable upstream module\n")
    (source / "Makefile").write_text(
        "SUBDIRS = auto_explain\n"
        "install:\n"
        "\tmkdir -p $(DESTDIR)/opt/probe/lib/postgresql\n"
        "\tprintf fixture > $(DESTDIR)/opt/probe/lib/postgresql/auto_explain.so\n"
    )
    (tmp_path / "obj/contrib").mkdir(parents=True)
    helper = tmp_path / "helper"
    helper.mkdir()

    def command(argv, **kwargs):
        argv = [arg.replace("/work/source/", str(helper) + "/") for arg in argv]
        log = tmp_path / "command.log"
        result = subprocess.run(argv, capture_output=True, text=True, check=True)
        log.write_text(result.stdout)
        return SimpleNamespace(stdout_log=log)

    branch = SimpleNamespace(
        helper=helper,
        source=tmp_path / "source",
        container_root=str(tmp_path),
        root=tmp_path,
        prefix="/opt/probe",
        output=tmp_path / "output",
        report={},
        command=command,
    )
    capture(branch, {})
    document = read_json(branch.output / "contrib-files.json")
    assert document["modules"] == ["auto_explain"]
    assert any(e["path"] == "lib/postgresql/auto_explain.so" for e in document["entries"])
    assert not (tmp_path / "contrib-stage").exists()


@pytest.mark.parametrize("profile", [None, *BUILD_PROFILES])
def test_every_bundled_profile_enables_complete_contrib(tmp_path, profile):
    initialize_project(tmp_path, profile=profile)
    config = load_config(tmp_path / "build.yaml").data
    assert config["defaults"]["packaging"]["standard_contrib"] is True
    for variant in config["variants"]:
        assert all(variant["build"]["options"][name] for name in REQUIRED_OPTIONS)
        assert "--with-selinux" in variant["build"]["configure_args"]
    manifest = tmp_path / "build.yaml"
    value = yaml.safe_load(manifest.read_text())
    value["spec"].setdefault("build", {}).setdefault("options", {})["python"] = False
    manifest.write_text(yaml.safe_dump(value))
    with pytest.raises(ValidationError, match="standard contrib requires"):
        load_config(manifest)


@pytest.mark.parametrize("damage", ["removed", "modified", "missing-inventory"])
def test_contrib_is_not_silently_lost_or_overwritten(tmp_path, damage):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    library = prefix / "auto_explain.so"
    library.write_text("original")
    output = tmp_path / "output"
    write_json(output / "contrib-files.json", {**inventory(prefix), "modules": ["auto_explain"]})
    if damage == "removed":
        library.unlink()
    elif damage == "modified":
        library.write_text("changed")
    else:
        (output / "contrib-files.json").unlink()
    branch = SimpleNamespace(
        output=output, config={"defaults": {"packaging": {"standard_contrib": True}}}
    )
    files = {e["path"]: e for e in inventory(prefix)["entries"]}
    with pytest.raises(PreconditionError, match="standard contrib"):
        selected_files(branch, files, lambda _: None)


def test_configured_contrib_cannot_omit_optional_source_directory(tmp_path):
    for module in ("auto_explain", "uuid-ossp"):
        path = tmp_path / "source/contrib" / module
        path.mkdir(parents=True)
        (path / "Makefile").write_text("fixture")
    log = tmp_path / "make.log"
    log.write_text("PG_MAKER_CONTRIB=auto_explain\n")
    branch = SimpleNamespace(
        helper=tmp_path,
        source=tmp_path / "source",
        container_root="/work/build/test",
        command=lambda *a, **k: SimpleNamespace(stdout_log=log),
    )
    with pytest.raises(PreconditionError, match="omits upstream modules"):
        capture(branch, {})


def test_installed_check_rejects_omission_even_if_package_inventory_is_consistent(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "auto_explain.so").write_text("original")
    document = {**inventory(prefix), "modules": ["auto_explain"]}
    (prefix / "auto_explain.so").unlink()
    harness = Harness(
        {
            "roles": ["server"],
            "inventory": [],
            "standard_contrib": document,
            "service": {"prefix": str(prefix), "data": str(tmp_path / "data")},
        },
        tmp_path / "result",
    )
    with pytest.raises(RuntimeError, match="standard-contrib-file:auto_explain"):
        harness.installed_inventory()
