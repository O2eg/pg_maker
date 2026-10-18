"""Real ownership/cancellation checks at the build-engine/runner boundary."""

import os
import signal
import subprocess
import time
from functools import lru_cache
from pathlib import Path

import pytest
import yaml

from pg_maker.api import MakerService
from pg_maker.assets import initialize_project
from pg_maker.config import environment_requirements, load_config
from pg_maker.planning.documents import seal
from pg_maker.planning.runtime_repository import INVENTORY_SCRIPT, REPOSITORY
from pg_maker.runners.docker import current_user
from pg_maker.storage import content_hash, read_json, write_json

pytestmark = pytest.mark.docker


@lru_cache
def image_repository(image):
    import json

    files = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python3",
                image,
                "-c",
                INVENTORY_SCRIPT,
            ],
            text=True,
        )
    )
    return (
        {"path": REPOSITORY, "files": files, "repository_hash": content_hash(files)}
        if files
        else None
    )


def prepared_build(tmp_path, configure, *, source_files=None, extra_feature=False):
    image = os.environ.get("PG_MAKER_BUILD_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_BUILD_IMAGE to a prepared image with the build user")
    initialize_project(tmp_path)
    feature_path = tmp_path / "config/features/my-feature.yaml"
    feature = yaml.safe_load(feature_path.read_text())
    feature["spec"]["components"] = []  # Minimal synthetic source, no external downloads.
    feature_path.write_text(yaml.safe_dump(feature, sort_keys=False))
    source = tmp_path / "source"
    source.mkdir()
    (source / "configure.ac").write_text("AC_INIT([PostgreSQL], [18.1], [test])\n")
    (source / "configure").write_text(
        configure
        + """
mkdir -p src/include
touch src/include/pg_config.h
for arg do
    case "$arg" in
        --prefix=*) printf 'prefix := %s\\n' "${arg#--prefix=}" >> src/Makefile.global ;;
    esac
done
cat >> src/Makefile.global <<'SETTINGS'
enable_tap_tests = yes
enable_nls = yes
with_icu = yes
with_llvm = yes
with_ssl = openssl
SETTINGS
"""
    )
    (source / "configure").chmod(0o755)
    for name, content in (source_files or {}).items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(content)
    manifest = yaml.safe_load((tmp_path / "build.yaml").read_text())
    manifest["spec"]["source"] = {
        "kind": "snapshot",
        "path": "./source",
        "include": ["**"],
        "exclude": [],
    }
    if extra_feature:
        feature = yaml.safe_load((tmp_path / "config/features/my-feature.yaml").read_text())
        feature["metadata"]["id"] = "other"
        feature["spec"]["numeric_id"] = None
        (tmp_path / "config/features/other.yaml").write_text(yaml.safe_dump(feature))
        manifest["spec"]["features"].append("other")
    (tmp_path / "build.yaml").write_text(yaml.safe_dump(manifest))
    (tmp_path / "config/defaults.yaml").write_text(
        yaml.safe_dump(
            {
                "spec": {
                    "common_changes": [],
                    "build": {"jobs": min(4, len(os.sched_getaffinity(0)))},
                    "execution": {
                        "build_user": current_user(),
                        "limits": {"min_free_disk_bytes": 1},
                    },
                }
            }
        )
    )
    config = load_config(tmp_path / "build.yaml")
    requirements = environment_requirements(config.data)
    images = [
        {
            "id": r["id"],
            "target": r["target"],
            "role": r["role"],
            "image_id": image,
            "base_image_id": image,
            "base_registry_digests": [],
            "registry_manifest_digest": None,
            "image_archive_sha256": None,
            "os": r["os"],
            "architecture": r["architecture"],
            "packages": [],
            "tools": {},
            "method": "apt-commit",
            "reproducibility": "cancellation fixture",
            **({"runtime_repository": image_repository(image)} if r["role"] == "build" else {}),
        }
        for r in requirements["environments"]
    ]
    write_json(
        tmp_path / "env.json",
        seal("environment", {"requirements_hash": content_hash(requirements), "images": images}),
    )
    service = MakerService(tmp_path / "operations")
    state = service.resolve(
        tmp_path / "build.yaml",
        environments=tmp_path / "env.json",
        output=tmp_path / "lock.json",
        input_store=tmp_path / "inputs",
    )
    assert (
        service.wait(service.directory / state["operation_id"], timeout_seconds=20)["status"]
        == "succeeded"
    )
    return service


@pytest.mark.parametrize("kill_worker", [False, True])
def test_build_cancel_and_lost_worker_cleanup(tmp_path, kill_worker):
    service = prepared_build(tmp_path, "#!/bin/sh\nsleep 45\nexit 1\n")
    state = service.build(tmp_path / "lock.json")
    directory = service.directory / state["operation_id"]
    try:
        deadline = time.monotonic() + 20
        while not (
            directory / "attempts/1/logs/my-feature.debian13-x64.candidate.2.stdout.log"
        ).exists():
            actual = service.status(directory)
            assert actual["status"] in {"planned", "running"}, actual
            assert time.monotonic() < deadline
            time.sleep(0.05)
        if kill_worker:
            os.kill(state["owner"]["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 10
            while service.status(directory)["status"] != "interrupted":
                assert time.monotonic() < deadline
                time.sleep(0.05)
        service.cancel(directory)
        result = service.wait(directory, timeout_seconds=30)
        assert result["status"] == ("interrupted" if kill_worker else "cancelled"), result
        assert not result["resources"]
        token = read_json(directory / "state.json")["owner_token"]
        assert not subprocess.check_output(
            ["docker", "ps", "-aq", "--filter", "label=io.pg-maker.owner=" + token], text=True
        ).strip()
    finally:
        service.cancel(directory)
        service.wait(directory, timeout_seconds=30)


def test_fast_build_runs_to_completion_without_database_tests(tmp_path):
    # A tiny Make fixture exercises the real worker/container/export path without PostgreSQL.
    service = prepared_build(
        tmp_path,
        """#!/bin/sh
set -eu
mkdir -p src
echo 'enable_tap_tests = yes' > src/Makefile.global
for arg do
    case "$arg" in --prefix=*) printf 'prefix := %s\\n' "${arg#--prefix=}" > Makefile ;; esac
done
cat >> Makefile <<'MAKE'
.PHONY: all install world-bin install-world-bin check check-world
all world-bin:
\t@echo built > artifact
install install-world-bin: all
\tmkdir -p $(prefix)/bin
\tcp artifact $(prefix)/bin/fixture
check check-world:
\t@echo 'DB test must not run' >&2
\texit 99
MAKE
""",
    )
    started = service.build(tmp_path / "lock.json")
    directory = service.directory / started["operation_id"]
    try:
        state = service.wait(directory, timeout_seconds=30)
        assert state["status"] == "succeeded", state
        assert state["result"]["qualification"] == "unverified"
        assert state["result"]["db_tests"] == "skipped"
        assert state["result"]["publication_eligible"] is False
        assert not state["resources"]
        report_path = state["result"]["reports"][0]["path"]
        report = read_json(Path(report_path))
        assert report["qualification"] == "unverified"
        assert report["db_tests"] == "skipped"
        assert "verified_test_ids" not in report and "world_tests" not in report
        assert (Path(report["build_prefix"]) / "bin/fixture").read_text() == "built\n"
        assert not list((directory / "workspace/build").glob("*/clusters"))
    finally:
        service.cancel(directory)
        service.wait(directory, timeout_seconds=30)


def test_container_reaps_orphan_processes(tmp_path):
    from pg_maker.models import CommandSpec, EnvironmentSpec, OperationRequest

    image = os.environ.get("PG_MAKER_BUILD_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_BUILD_IMAGE to a prepared Perl image")
    service = MakerService(tmp_path / "operations")
    request = OperationRequest(
        EnvironmentSpec(image, current_user()),
        (
            CommandSpec(
                "orphan",
                (
                    "perl",
                    "-e",
                    "my $pid = fork; die $! unless defined $pid; "
                    "if (!$pid) {sleep 1; exit 0} "
                    'open(my $f, ">", "/work/build/pid") or die $!; print $f $pid; close $f;',
                ),
            ),
            CommandSpec(
                "reaped",
                (
                    "perl",
                    "-e",
                    'open(my $f, "<", "/work/build/pid") or die $!; my $pid = <$f>; '
                    "for (1..30) {exit 0 unless kill(0, $pid); select undef, undef, undef, 0.1} "
                    'die "orphan process was not reaped";',
                ),
            ),
        ),
    )
    state = service.start_operation(request)
    result = service.wait(service.directory / state["operation_id"], timeout_seconds=20)
    assert result["status"] == "succeeded", result
