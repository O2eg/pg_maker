"""Small native build fixture; no Docker, network or PostgreSQL installation."""

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from pg_maker.builders.components import build_components


@pytest.mark.skipif(
    not shutil.which("cmake") or not shutil.which("cc"),
    reason="native component fixture needs CMake and a C compiler",
)
def test_cmake_installs_from_frozen_auxiliary_source_and_checks_control(tmp_path):
    source, extra = tmp_path / "input", tmp_path / "extra"
    source.mkdir()
    extra.mkdir()
    (extra / "probe.c").write_text("int probe(void) { return 70; }\n")
    (source / "probe.control").write_text("default_version = '1.0'\n")
    (source / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\nproject(probe C)\n"
        "add_library(probe SHARED ${EXTRA_SOURCE}/probe.c)\n"
        "install(TARGETS probe DESTINATION lib/postgresql)\n"
        "install(FILES probe.control DESTINATION share/postgresql/extension)\n"
    )
    root = tmp_path / "build"
    root.mkdir()
    (root / "components").mkdir()
    prefix = root / "prefix"
    prefix.mkdir()
    commands = []

    def command(argv, **kwargs):
        commands.append(argv)
        subprocess.run(
            argv,
            cwd=kwargs["cwd"],
            check=True,
            capture_output=True,
            env={**os.environ, "CMAKE_BUILD_PARALLEL_LEVEL": "1", **kwargs.get("env", {})},
        )

    branch = SimpleNamespace(
        root=root,
        output=tmp_path / "output",
        container_root=str(root),
        prefix=str(prefix),
        inputs=SimpleNamespace(tree=lambda digest: source if digest == "main" else extra),
        variant={"build": {"jobs": 1}},
        report={},
        command=command,
    )
    component = {
        "metadata": {"id": "probe"},
        "spec": {
            "type": "library",
            "build_system": "cmake",
            "source": {"source_hash": "main"},
            "extra_sources": {"fixture": {"source_hash": "auxiliary"}},
            "configure_args": ["-DEXTRA_SOURCE={source:fixture}"],
            "sql_extension_versions": {"probe": "1.0"},
        },
    }
    build_components(branch, [component], run_tests=False)
    assert (prefix / "lib/postgresql/libprobe.so").is_file()
    assert (root / "install-owners/probe.json").is_file()
    assert not (root / "install-staging/probe").exists()
    assert branch.report["components"][0]["tests"] == "skipped"
    assert "-DEXTRA_SOURCE=" + str(root / "extra-sources/probe/fixture") in commands[0]
    assert (root / "extra-sources/probe/fixture/probe.c").read_bytes() == (
        extra / "probe.c"
    ).read_bytes()
