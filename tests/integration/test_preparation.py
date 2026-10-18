import os
import signal
import subprocess
import time

import pytest
import yaml

from pg_maker.api import MakerService
from pg_maker.assets import initialize_project
from pg_maker.config import resource_path
from pg_maker.errors import PreconditionError
from pg_maker.runners.docker import current_user
from pg_maker.storage import read_json
from pg_maker.yamlio import load_yaml

pytestmark = pytest.mark.docker


def fixture_tools(delay):
    # Lifecycle fixture only: no source compilation, deterministic tool probes.
    return {
        "delay": ["sleep", delay],
        **{
            name: ["printf", version]
            for name, version in {
                "make": "4.4",
                "perl": "5.40",
                "bison": "3.8",
                "flex": "2.6",
                "llvm": "19.1",
                "clang": "19.1",
            }.items()
        },
    }


@pytest.fixture
def preparation(tmp_path):
    image = os.environ.get("PG_MAKER_PREPARATION_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_PREPARATION_IMAGE to a local Debian 13 image")
    initialize_project(tmp_path)
    recipe = load_yaml(resource_path("recipes/platforms/debian-13.yaml"))
    recipe["spec"]["replace_builtin"] = True
    recipe["spec"]["option_dependencies"] = {key: [] for key in ("llvm", "openssl", "icu")}
    recipe["spec"]["runtime_dependencies"] = {}
    recipe["spec"]["service_dependencies"] = {}
    for environment in recipe["spec"]["environments"].values():
        environment.update(image=image, packages=[], tools=fixture_tools("30"))
    (tmp_path / "config/platforms").mkdir()
    (tmp_path / "config/platforms/debian-13.yaml").write_text(yaml.safe_dump(recipe))
    defaults = {
        "spec": {
            "execution": {"limits": {"min_free_disk_bytes": 1}},
            "validation": {"database_locales": []},
        }
    }
    (tmp_path / "config/defaults.yaml").write_text(yaml.safe_dump(defaults))
    service = MakerService(tmp_path / "operations")
    state = service.prepare_environments(
        tmp_path / "build.yaml",
        output=tmp_path / "env.json",
        input_store=tmp_path / "inputs",
        offline=True,
    )
    directory = service.directory / state["operation_id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = service.status(directory)
        assert state["status"] in {"planned", "running"}, state
        if (directory / "attempts/1/logs/debian13-x64.build.3.stdout.log").exists():
            break
        time.sleep(0.05)
    else:
        service.cancel(directory)
        pytest.fail("preparation did not reach the slow tool inventory command")
    yield service, directory, state
    service.cancel(directory)
    service.wait(directory, timeout_seconds=30)


@pytest.mark.parametrize("kill_worker", [False, True])
def test_preparation_cancel_and_lost_worker_cleanup(preparation, kill_worker):
    service, directory, state = preparation
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
    assert not (directory.parent.parent / "env.json").exists()
    actual = read_json(directory / "state.json")
    assert not subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", "label=io.pg-maker.owner=" + actual["owner_token"]],
        text=True,
    ).strip()


def test_preparation_resume_reuses_completed_image(tmp_path):
    image = os.environ.get("PG_MAKER_PREPARATION_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_PREPARATION_IMAGE to run preparation recovery")
    initialize_project(tmp_path)
    recipe = load_yaml(resource_path("recipes/platforms/debian-13.yaml"))
    recipe["spec"]["replace_builtin"] = True
    recipe["spec"]["option_dependencies"] = {k: [] for k in ("llvm", "openssl", "icu")}
    recipe["spec"]["option_runtime_dependencies"] = {}
    recipe["spec"]["runtime_dependencies"] = {}
    recipe["spec"]["service_dependencies"] = {}
    for environment in recipe["spec"]["environments"].values():
        environment.update(image=image, packages=[], tools=fixture_tools("3"))
    (tmp_path / "config/platforms").mkdir()
    (tmp_path / "config/platforms/debian-13.yaml").write_text(yaml.safe_dump(recipe))
    (tmp_path / "config/defaults.yaml").write_text(
        yaml.safe_dump(
            {
                "spec": {
                    "execution": {
                        "build_user": current_user(),
                        "limits": {"min_free_disk_bytes": 1},
                    },
                    "validation": {"database_locales": []},
                }
            }
        )
    )
    service = MakerService(tmp_path / "operations")
    started = service.prepare_environments(
        tmp_path / "build.yaml",
        output=tmp_path / "env.json",
        input_store=tmp_path / "inputs",
        offline=True,
    )
    directory = service.directory / started["operation_id"]
    try:
        deadline = time.monotonic() + 30
        while not (directory / "attempts/1/logs/debian13-x64.test.3.stdout.log").exists():
            assert service.status(directory)["status"] in {"planned", "running"}
            assert time.monotonic() < deadline
            time.sleep(0.05)
        first = read_json(directory / "prepared-images.json")["debian13-x64.build"]["image"][
            "image_id"
        ]
        service.cancel(directory)
        assert service.wait(directory, timeout_seconds=30)["status"] == "cancelled"
        with pytest.raises(PreconditionError):
            service.resume(directory, input_hash="sha256:" + "0" * 64)
        service.resume(directory, input_hash=started["input_hash"])
        result = service.wait(directory, timeout_seconds=30)
        assert result["status"] == "succeeded", result
        assert result["attempt"] == 2 and not result["resources"]
        images = read_json(tmp_path / "env.json")["spec"]["images"]
        assert next(i for i in images if i["role"] == "build")["image_id"] == first
        assert '"type":"environment_reused"' in (directory / "events.jsonl").read_text()
    finally:
        service.cancel(directory)
        service.wait(directory, timeout_seconds=30)
