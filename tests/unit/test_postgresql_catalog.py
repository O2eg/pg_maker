import subprocess

import pytest
import test_planning
from test_planning import resolved, write_yaml

from pg_maker.builders.autotools import validate_build
from pg_maker.changes.apply import apply_patch, socket_patch
from pg_maker.config import compatible, environment_requirements, load_config
from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.planning.plan import build_plan
from pg_maker.postgresql import recipe, verify_tools, version_info
from pg_maker.yamlio import load_yaml

project = test_planning.project


def select_version(project, version):
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["postgresql"]["version"] = version
    manifest["spec"]["documentation"]["enabled"] = True
    write_yaml(project / "build.yaml", manifest)
    (project / "source/configure.ac").write_text(f"AC_INIT([PostgreSQL], [{version}])\n")
    return load_config(project / "build.yaml").data


@pytest.mark.parametrize(
    "version", ["14.24", "15.19", "16.15", "17.11", "18.6", "19beta4", "19rc1", "19.0"]
)
def test_major_selection_and_documentation_are_pinned(project, version):
    config = select_version(project, version)
    selected = config["postgresql_recipe"]
    assert selected["major"] == version_info(version)["major"]
    assert selected["documentation"]["format"] == "docbook-xml-4.5"
    assert selected["c_standard"] == ("c11" if version.startswith("19") else "c99")
    assert config["documentation"]["enabled"]
    requirements = environment_requirements(config)["environments"][0]
    assert requirements["tool_minimums"]["make"] == "4.0"
    assert "xmllint" in requirements["tools"]
    assert "clang" in requirements["tools"]
    assert requirements["tool_minimums"]["llvm"] == selected["llvm_minimum"]


def test_prerelease_identity_and_native_package_order(project):
    versions = []
    for version in ("19beta4", "19rc1", "19.0"):
        config = select_version(project, version)
        identity = config["variants"][0]["identity"]
        assert identity["paths"]["runtime_prefix"] == "/opt/test/my-feature/19"
        assert identity["service"] == "test-my-feature-server-19"
        assert "~" not in identity["oci_tag"]
        versions.append(identity["packages"][0]["deb_version"])
    assert versions == ["19.0.0~beta4-1.deb13", "19.0.0~rc1-1.deb13", "19.0.0-1.deb13"]
    for before, after in zip(versions[:-1], versions[1:], strict=True):
        subprocess.run(["dpkg", "--compare-versions", before, "lt", after], check=True)
    compatible("19beta4", ">=14,<20", "platform")
    with pytest.raises(UnsupportedError):
        compatible("19beta4", ">=18,<19", "extension")


def test_unknown_major_and_unavailable_option_are_early_errors(project):
    with pytest.raises(UnsupportedError, match="below 14"):
        version_info("13.23")
    with pytest.raises(UnsupportedError, match="no build recipe"):
        recipe("20.0")
    for invalid in ("19", "19devel", "19.0beta1", "19rc0", "019.1"):
        with pytest.raises(ValidationError):
            version_info(invalid)
    select_version(project, "14.24")
    manifest = load_yaml(project / "build.yaml")
    manifest["spec"]["build"] = {"options": {"zstd": True}}
    write_yaml(project / "build.yaml", manifest)
    with pytest.raises(UnsupportedError, match="unavailable"):
        load_config(project / "build.yaml")


def test_toolchain_minimums_use_container_evidence():
    minimums = {"make": "4.0", "perl": "5.14", "llvm": "14"}
    current = {"make": "GNU Make 4.4.1", "perl": "version='5.40.1';", "llvm": "19.1.7"}
    assert verify_tools(minimums, current)["perl"] == "5.40.1"
    for override in ({"make": "GNU Make 3.81"}, {"llvm": "13.0.1"}, {"perl": "missing"}):
        with pytest.raises(PreconditionError, match="build tool"):
            verify_tools(minimums, {**current, **override})


def test_beta_lock_and_plan_do_not_promise_database_qualification(project):
    config = select_version(project, "19beta4")
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state
    assert lock["spec"]["config"]["postgresql_recipe"] == config["postgresql_recipe"]
    plan = build_plan(lock, goal="package")
    assert not plan["spec"]["run_db_tests"]
    assert any(s["kind"] == "build_docs_en" for s in plan["spec"]["steps"])
    with pytest.raises(UnsupportedError, match="DB-test evidence"):
        validate_build(config, "verified", run_db_tests=True)


@pytest.mark.parametrize("line", [193, 198, 210])
def test_common_patch_is_pinned_at_the_actual_source_line(tmp_path, line):
    header = tmp_path / "src/include/pg_config_manual.h"
    header.parent.mkdir(parents=True)
    header.write_text(
        "/* other settings */\n" * (line - 1) + '#define DEFAULT_PGSOCKET_DIR  "/tmp"\n'
    )
    patch = socket_patch(tmp_path)
    assert f"@@ -{line} +{line} @@" in patch
    apply_patch(tmp_path, patch)
    assert header.read_text().endswith(
        '#define DEFAULT_PGSOCKET_DIR "/var/run/postgresql" /* Shared by server and libpq. */\n'
    )
    assert socket_patch(tmp_path) == patch
    header.write_text(header.read_text() * 2)
    with pytest.raises(PreconditionError, match="exact"):
        socket_patch(tmp_path)


def test_common_patch_normalizes_an_existing_socket_default(tmp_path):
    header = tmp_path / "src/include/pg_config_manual.h"
    header.parent.mkdir(parents=True)
    header.write_text(
        '#ifndef WIN32\n#define DEFAULT_PGSOCKET_DIR  "/var/run/postgresql"\n'
        '#else\n#define DEFAULT_PGSOCKET_DIR ""\n#endif\n'
    )
    apply_patch(tmp_path, socket_patch(tmp_path))
    assert header.read_text() == (
        '#ifndef WIN32\n#define DEFAULT_PGSOCKET_DIR "/var/run/postgresql" '
        "/* Shared by server and libpq. */\n"
        '#else\n#define DEFAULT_PGSOCKET_DIR ""\n#endif\n'
    )


def test_bundled_socket_patch_matches_the_source_specific_generator(tmp_path):
    from pg_maker.config import resource_path

    header = tmp_path / "src/include/pg_config_manual.h"
    header.parent.mkdir(parents=True)
    header.write_text("\n" * 192 + '#define DEFAULT_PGSOCKET_DIR  "/tmp"\n')
    assert resource_path("bundled", "patches", "unix-socket-directory.patch").read_text() == (
        socket_patch(tmp_path)
    )


@pytest.mark.parametrize(
    "version,expected",
    [
        ("19beta3", False),
        ("19beta4", True),
        ("19rc1", True),
        ("19rc2", False),
        ("19.0", False),
        ("18.6", False),
    ],
)
def test_doc_fix_is_bounded_and_part_of_effective_config(project, version, expected):
    config = select_version(project, version)
    assert bool(config["documentation"]["rules"]) is expected


def test_materialized_vpath_generated_pair_does_not_create_empty_c_file(tmp_path):
    import os

    from pg_maker.sources.store import materialize

    source, copied, obj = (tmp_path / name for name in ("source", "copied", "obj"))
    source.mkdir()
    (source / "names.c").write_text('const char *names[] = {"lock"};\n')
    (source / "names.h").write_text("extern const char *names[];\n")
    os.utime(source / "names.c", (100, 100))
    os.utime(source / "names.h", (200, 200))
    materialize(source, copied)
    obj.mkdir()
    (obj / "Makefile").write_text(
        f"VPATH={copied}\nall: names.c\n\t@test -s $<\nnames.c: names.h\n\ttouch $@\n"
    )
    subprocess.run(["make", "-s"], cwd=obj, check=True, capture_output=True)
    assert not (obj / "names.c").exists()
    assert (source / "names.h").stat().st_mtime == 200
    # Subsequent source changes must still cause native dependency rules to run.
    (copied / "names.h").write_text("/* changed generator output */\n")
    result = subprocess.run(["make", "-s"], cwd=obj, capture_output=True)
    assert result.returncode != 0
    assert (obj / "names.c").exists()
