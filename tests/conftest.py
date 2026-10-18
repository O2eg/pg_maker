import os

import pytest

from pg_maker.api import MakerService
from pg_maker.models import CommandSpec, EnvironmentSpec, OperationRequest
from pg_maker.runners.docker import current_user


@pytest.fixture
def request_document():
    return OperationRequest(
        EnvironmentSpec("sha256:" + "a" * 64, current_user()),
        (CommandSpec("test", ("uname", "-s")),),
    ).to_document()


@pytest.fixture
def docker_image():
    image = os.environ.get("PG_MAKER_DOCKER_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_DOCKER_IMAGE to an existing immutable local image")
    return image


@pytest.fixture
def operations(tmp_path, docker_image):
    service = MakerService(tmp_path / "operations")
    directories = []

    def start(commands, *, outputs=(), request_id=None):
        request = OperationRequest(
            EnvironmentSpec(docker_image, current_user()), tuple(commands), tuple(outputs)
        )
        state = service.start_operation(request, request_id=request_id)
        directory = service.directory / state["operation_id"]
        directories.append(directory)
        return service, directory, state

    yield start
    for directory in directories:
        service.cancel(directory)
        service.wait(directory, timeout_seconds=30)
