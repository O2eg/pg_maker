import shutil
import subprocess
from pathlib import Path

import pytest

from pg_maker.assets import BUILD_PROFILES, initialize_project
from pg_maker.config import load_config
from pg_maker.errors import PreconditionError
from pg_maker.packaging.layout import normalize_install_layout
from pg_maker.packaging.service import service_files


@pytest.mark.parametrize("profile", BUILD_PROFILES)
@pytest.mark.parametrize("mode", ["release", "debug"])
def test_managed_layout_reaches_service_and_stays_idempotent(tmp_path, profile, mode):
    initialize_project(tmp_path, profile=profile, build_mode=mode)
    assert initialize_project(tmp_path, profile=profile, build_mode=mode)["created"] == []
    config = load_config(tmp_path / "build.yaml").data
    major = config["postgresql"]["version"].split(".")[0]
    suffix = "-debug" if mode == "debug" else ""
    variant = config["variants"][0]
    identity = variant["identity"]
    prefix = f"/opt/postgresql-yc-clone/{major}{suffix}"
    data = f"/var/lib/postgresql-yc-clone/{major}{suffix}/data"
    service = f"postgresql-yc-clone-{major}{suffix}"
    assert identity["paths"]["runtime_prefix"] == prefix
    assert identity["paths"]["data_directory"] == data
    assert identity["paths"]["documentation_directory"].startswith(prefix + "/share/doc/")
    assert identity["service"] == service
    unit, _ = service_files(config, variant)[f"usr/lib/systemd/system/{service}.service"]
    assert f"Environment=PGDATA={data}" in unit
    assert f"ExecStart={prefix}/bin/pg_ctl start -D {data} " in unit


@pytest.fixture
def source(tmp_path):
    (tmp_path / "src").mkdir()
    shutil.copyfile(
        Path(__file__).parents[1] / "fixtures/layout/Makefile.global.in",
        tmp_path / "src/Makefile.global.in",
    )
    return tmp_path


@pytest.mark.parametrize("prefix", ["/opt/postgresql-yc-clone/18", "/opt/pgsql/17", "/opt/test/16"])
def test_make_keeps_private_subdirectories_independent_of_prefix(source, prefix):
    receipt = source / "receipt.patch"
    before = (source / "src/Makefile.global.in").read_bytes()
    report = normalize_install_layout(source, receipt)
    output = subprocess.check_output(
        ["make", "-s", "-f", "src/Makefile.global.in", "show", "prefix=" + prefix],
        cwd=source,
        text=True,
    ).split()
    assert output == [
        prefix + "/" + path + "/postgresql"
        for path in ("share", "etc", "lib", "include", "share/doc")
    ]
    patched = (source / "src/Makefile.global.in").read_bytes()
    assert normalize_install_layout(source, receipt) == report
    assert (source / "src/Makefile.global.in").read_bytes() == patched
    subprocess.run(["patch", "-R", "-p1", "-i", str(receipt)], cwd=source, check=True)
    assert (source / "src/Makefile.global.in").read_bytes() == before


def test_already_suffixed_directories_are_not_doubled(source):
    normalize_install_layout(source, source / "receipt.patch")
    output = subprocess.check_output(
        [
            "make",
            "-s",
            "-f",
            "src/Makefile.global.in",
            "show",
            "pkglibdir=/opt/vendor/lib/postgresql",
        ],
        cwd=source,
        text=True,
    ).split()
    assert output[2] == "/opt/vendor/lib/postgresql"


def test_unknown_layout_fails_without_partial_edit(source):
    path = source / "src/Makefile.global.in"
    original = path.read_text().replace("$(findstring postgres, $(docdir))", "unknown-rule")
    path.write_text(original)
    with pytest.raises(PreconditionError, match="unsupported PostgreSQL"):
        normalize_install_layout(source, source / "receipt.patch")
    assert path.read_text() == original
    assert not (source / "receipt.patch").exists()
