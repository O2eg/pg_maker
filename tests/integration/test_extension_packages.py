"""Opt-in real extension package build against an already prepared, pinned SDK."""

import os
import time
from pathlib import Path

import pytest

from pg_maker.api import MakerService
from pg_maker.artifacts.catalog import operation_artifacts
from pg_maker.extensions.verification import verify_extension_output
from pg_maker.storage import read_json

pytestmark = pytest.mark.docker


def test_cancel_after_source_tests_then_resume_reuses_components(tmp_path):
    spelling = os.environ.get("PG_MAKER_EXTENSION_LOCK")
    if not spelling:
        pytest.skip("set PG_MAKER_EXTENSION_LOCK to a prepared three-extension lock")
    service = MakerService(tmp_path / "operations")
    state = service.extension_build(Path(spelling).absolute())
    operation = service.directory / state["operation_id"]
    deadline = time.monotonic() + 3600
    cursor = 0
    reached = False
    try:
        while time.monotonic() < deadline:
            events = service.events(operation, after_sequence=cursor, limit=1000)["events"]
            if events:
                cursor = events[-1]["sequence"]
            if any(
                e["type"] == "extension_stage_started" and e["step_id"] == "test-packages"
                for e in events
            ):
                reached = True
                break
            assert service.status(operation)["status"] not in {"failed", "succeeded", "cancelled"}
            time.sleep(0.1)
    finally:
        service.cancel(operation)
        stopped = service.wait(operation, timeout_seconds=60)
    assert reached and stopped["status"] == "cancelled"
    service.resume(operation, input_hash=stopped["input_hash"])
    completed = service.wait(operation, timeout_seconds=3600)
    assert completed["status"] == "succeeded", completed
    report = read_json(operation / "workspace/output/report.json")
    assert report["qualification"] == "extension-package-verified"
    builds = [t for t in report["timings"] if t["stage"] == "build"]
    assert len(builds) == 3 and all(t["reused"] for t in builds)
    assert report["package_tests"]["remove_reinstall_preserves_data"]
    verify_extension_output(operation / "workspace/output")


def test_three_extensions_with_native_dependencies_and_clean_install(tmp_path):
    spelling = os.environ.get("PG_MAKER_EXTENSION_LOCK")
    if not spelling:
        pytest.skip("set PG_MAKER_EXTENSION_LOCK to a prepared three-extension lock")
    service = MakerService(tmp_path / "operations")
    state = service.extension_build(Path(spelling).absolute())
    operation = service.directory / state["operation_id"]
    completed = service.wait(operation, timeout_seconds=3600)
    assert completed["status"] == "succeeded", completed
    assert not completed["resources"]
    output = operation / "workspace/output"
    verify_extension_output(output)
    report = read_json(output / "report.json")
    assert report["qualification"] == "extension-package-verified"
    assert len(report["packages"]) == 3
    tests = report["package_tests"]
    assert tests["passed"] and tests["installed_inventory"]
    assert tests["restart"] and tests["remove_reinstall_preserves_data"]
    assert all(t["missing_server_rejected"] for t in tests["negative_dependencies"])
    assert {t["extension"] for t in tests["extensions"] if t["functional"]} == {
        "pg_repack",
        "pg_wait_sampling",
        "pg_stat_kcache",
    }
    source = read_json(output / "source-tests.json")
    assert source["passed"] and all(s["count"] > 0 for s in source["suites"])
    assert (
        len(
            [
                r
                for r in operation_artifacts(operation, completed)["artifacts"]
                if r["role"] == "package"
            ]
        )
        >= 3
    )
    service.export(operation, tmp_path / "delivery", include_inputs=True)
    verify_extension_output(tmp_path / "delivery/output")
    assert not (operation / "workspace/build").exists()
