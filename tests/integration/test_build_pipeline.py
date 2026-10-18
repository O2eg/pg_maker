"""Opt-in, complete PostgreSQL build from an already resolved real BuildLock."""

import os
from pathlib import Path

import pytest

from pg_maker.api import MakerService
from pg_maker.storage import file_hash, read_json

pytestmark = pytest.mark.docker


def test_real_postgresql_build_and_baseline(tmp_path):
    path = os.environ.get("PG_MAKER_BUILD_LOCK")
    if not path:
        pytest.skip("set PG_MAKER_BUILD_LOCK to run the full, long PostgreSQL build")
    lock = read_json(Path(path))
    service = MakerService(tmp_path / "operations")
    state = service.build(Path(path), run_db_tests=True)
    completed = service.wait(service.directory / state["operation_id"], timeout_seconds=43200)
    assert completed["status"] == "succeeded", completed
    assert completed["result"]["qualification"] == "source-verified"
    assert completed["result"]["publication_eligible"] is False
    assert not completed["resources"]
    expected_count = sum(2 if v["baseline"] else 1 for v in lock["spec"]["config"]["variants"])
    assert len(completed["result"]["reports"]) == expected_count
    for artifact in completed["result"]["reports"]:
        path = Path(artifact["path"])
        assert file_hash(path) == artifact["sha256"]
        report = read_json(path)
        assert report["core_tests"]["passed"] and report["core_tests"]["suites"] > 0
        assert report["world_tests"]["passed"] and report["world_tests"]["suites"] > 1
        assert report["qualification"] == "source-verified"
        assert all(t["passed"] for t in report.get("acceptance", []))
        assert all(t["passed"] for t in report.get("components", []))
        assert (Path(report["build_prefix"]) / "bin/postgres").is_file()
