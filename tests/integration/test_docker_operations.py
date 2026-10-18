import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from pg_maker.api import MakerService
from pg_maker.errors import OwnershipError, PreconditionError
from pg_maker.models import CommandSpec, EnvironmentSpec, OperationRequest
from pg_maker.runners.docker import DockerRunner, current_user
from pg_maker.storage import read_json

pytestmark = pytest.mark.docker


def wait_for_command(service, directory):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = service.status(directory)
        assert state["status"] in {"planned", "running"}, state
        events = service.events(directory)["events"]
        if any(event["type"] == "command_started" for event in events):
            return state
        time.sleep(0.05)
    raise AssertionError("command did not start")


def containers(owner):
    return subprocess.check_output(
        ["docker", "ps", "-aq", "--filter", f"label=io.pg-maker.owner={owner}"],
        text=True,
    ).strip()


def test_real_outputs_and_resource_ownership(operations, tmp_path, docker_image):
    service, directory, initial = operations(
        [
            CommandSpec("copy", ("cp", "/etc/os-release", "/work/output/os-release")),
        ],
        outputs=("os-release",),
    )
    state = service.wait(directory, timeout_seconds=30)
    assert state["status"] == "succeeded", state
    artifact = state["result"]["artifacts"][0]
    assert (directory / artifact["path"]).is_file()
    assert artifact["size_bytes"] > 0
    assert not state["resources"] and not containers(initial["owner_token"])
    events = service.events(directory)["events"]
    assert any(event["type"] == "environment_preparing" for event in events)
    assert (directory / "attempts/1/progress.log").is_file()


@pytest.mark.parametrize(
    "command,outputs",
    [
        (CommandSpec("failure", ("false",)), ()),
        (CommandSpec("missing", ("true",)), ("missing",)),
        (CommandSpec("timeout", ("sleep", "30"), timeout_seconds=1), ()),
    ],
)
def test_failure_timeout_and_missing_output_never_succeed(operations, command, outputs):
    service, directory, initial = operations([command], outputs=outputs)
    state = service.wait(directory, timeout_seconds=30)
    assert state["status"] == "failed"
    assert state["error"]["code"] == "command_failed"
    assert not containers(initial["owner_token"])


def test_cancel_stops_running_container_and_is_idempotent(operations):
    service, directory, initial = operations([CommandSpec("long", ("sleep", "30"))])
    wait_for_command(service, directory)
    service.cancel(directory)
    state = service.wait(directory, timeout_seconds=30)
    assert state["status"] == "cancelled", state
    assert service.cancel(directory)["status"] == "cancelled"
    assert not containers(initial["owner_token"])


def test_release_removes_image_declared_anonymous_volumes(operations):
    service, directory, initial = operations([CommandSpec("long", ("sleep", "30"))])
    wait_for_command(service, directory)
    container_id = containers(initial["owner_token"])
    inspected = json.loads(subprocess.check_output(["docker", "inspect", container_id], text=True))[
        0
    ]
    volumes = [m["Name"] for m in inspected["Mounts"] if m["Type"] == "volume"]
    if not volumes:
        pytest.skip("selected test image does not declare VOLUME")
    service.cancel(directory)
    assert service.wait(directory, timeout_seconds=30)["status"] == "cancelled"
    for name in volumes:
        result = subprocess.run(["docker", "volume", "inspect", name], capture_output=True)
        assert result.returncode != 0, "owned anonymous volume leaked after container removal"


def test_lost_worker_snapshot_recovery_and_cleanup(operations):
    service, directory, initial = operations([CommandSpec("long", ("sleep", "30"))])
    state = wait_for_command(service, directory)
    os.kill(state["owner"]["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = service.status(directory)
        if state["status"] == "interrupted":
            break
        time.sleep(0.05)
    assert state["status"] == "interrupted"
    assert containers(initial["owner_token"])
    (directory / "state.json").write_text("broken snapshot")
    assert service.status(directory)["status"] == "interrupted"
    assert read_json(directory / "state.json")["status"] == "interrupted"
    state = service.cancel(directory)
    assert state["status"] == "interrupted" and not state["resources"]
    assert not containers(initial["owner_token"])


def test_request_id_retry_and_changed_input_conflict(operations, docker_image):
    command = CommandSpec("long", ("sleep", "30"))
    service, directory, initial = operations([command], request_id="retry-1")
    request = OperationRequest(EnvironmentSpec(docker_image, current_user()), (command,))
    same = service.start_operation(request, request_id="retry-1")
    assert same["operation_id"] == initial["operation_id"]
    with pytest.raises(PreconditionError):
        service.start_operation(
            replace(request, commands=(CommandSpec("other", ("true",)),)), request_id="retry-1"
        )
    service.cancel(directory)


def test_foreign_labels_prevent_cleanup(operations):
    service, directory, initial = operations([CommandSpec("long", ("sleep", "30"))])
    wait_for_command(service, directory)
    request = OperationRequest.from_document(read_json(directory / "input.json"))
    runner = DockerRunner(
        directory, operation_id=initial["operation_id"], owner_token=initial["owner_token"]
    )
    handle = runner.intended_handle(request.environment)
    with pytest.raises(OwnershipError):
        runner.cancel(replace(handle, labels={**handle.labels, "io.pg-maker.run_id": "foreign"}))
    assert containers(initial["owner_token"])
    service.cancel(directory)


def test_secret_not_in_input_events_logs_or_result(operations, monkeypatch):
    secret = "stage12-secret-71c887"
    monkeypatch.setenv("PG_MAKER_TEST_CREDENTIAL", secret)
    service, directory, _ = operations(
        [
            CommandSpec(
                "secret", ("printenv", "TOKEN"), secret_env={"TOKEN": "PG_MAKER_TEST_CREDENTIAL"}
            ),
        ]
    )
    state = service.wait(directory, timeout_seconds=30)
    assert state["status"] == "succeeded", state
    assert state["result"]["commands"][0]["stdout_tail"] == "[REDACTED]\n"
    for path in directory.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path


def test_container_environment_cannot_redirect_docker_client(operations):
    service, directory, _ = operations(
        [
            CommandSpec(
                "env",
                ("/usr/bin/printenv", "DOCKER_HOST"),
                env={"DOCKER_HOST": "unix:///nonexistent", "PATH": "/nonexistent"},
            ),
        ]
    )
    state = service.wait(directory, timeout_seconds=30)
    assert state["status"] == "succeeded", state
    assert state["result"]["commands"][0]["stdout_tail"] == "unix:///nonexistent\n"


def test_worker_survives_parent_exit_and_waiter_interrupt(tmp_path, docker_image):
    root = tmp_path / "detached"
    code = (
        "import json; from pathlib import Path; from pg_maker.api import MakerService; "
        "from pg_maker.models import OperationRequest,EnvironmentSpec,CommandSpec; "
        "from pg_maker.runners.docker import current_user; "
        f"s=MakerService(Path({str(root)!r})); "
        f"r=OperationRequest(EnvironmentSpec({docker_image!r},current_user()),"
        "(CommandSpec('long',('sleep','30')),)); "
        "print(json.dumps(s.start_operation(r,operation_id='detached')))"
    )
    parent = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=10
    )
    assert parent.returncode == 0, parent.stderr
    initial = json.loads(parent.stdout)
    directory = root / initial["operation_id"]
    service = MakerService(root)
    try:
        state = wait_for_command(service, directory)
        assert state["owner"] is not None
        waiter = subprocess.Popen(
            [sys.executable, "-m", "pg_maker", "wait", str(directory), "--machine"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.5)
        waiter.send_signal(signal.SIGINT)
        stdout, stderr = waiter.communicate(timeout=10)
        assert waiter.returncode == 130, stderr
        assert json.loads(stdout)["error"]["code"] == "wait_interrupted"
        assert service.status(directory)["status"] == "running"
    finally:
        service.cancel(directory)
        service.wait(directory, timeout_seconds=30)
    assert not containers(initial["owner_token"])
