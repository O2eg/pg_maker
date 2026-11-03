import json
from types import SimpleNamespace

import pytest

from pg_maker.errors import PreconditionError
from pg_maker.planning.environments import inspect_base_image


class ImageStore:
    def __init__(self, root, target=None, offline=False):
        self.root, self.target, self.offline = root, target, offline
        self.pulls = []

    def _control(self, argv, **kwargs):
        image = self.target if "--platform" in argv else self.root
        return SimpleNamespace(succeeded=image is not None, stdout_tail=json.dumps(image))

    def command(self, argv):
        self.pulls.append(argv)
        self.target = {"Id": "child", "Os": "linux", "Architecture": "amd64"}


REQUIREMENT = {"image": "debian:13-slim", "architecture": "x86_64"}


def test_multiarch_index_retains_addressable_root_id():
    store = ImageStore(
        {"Id": "index", "Os": "", "Architecture": "", "RepoDigests": ["debian@digest"]},
        {"Id": "child", "Os": "linux", "Architecture": "amd64"},
    )
    image = inspect_base_image(store, store, REQUIREMENT)
    assert image["Id"] == "index"
    assert image["Architecture"] == "amd64"
    assert image["RepoDigests"] == ["debian@digest"]
    assert not store.pulls


def test_present_native_image_does_not_suppress_target_pull():
    store = ImageStore({"Id": "index", "Os": "linux", "Architecture": "arm64"})
    assert inspect_base_image(store, store, REQUIREMENT)["Architecture"] == "amd64"
    assert store.pulls == [["docker", "pull", "--platform", "linux/amd64", "debian:13-slim"]]


def test_offline_wrong_architecture_cannot_be_used():
    store = ImageStore({"Id": "native", "Os": "linux", "Architecture": "arm64"}, offline=True)
    with pytest.raises(PreconditionError, match="target platform"):
        inspect_base_image(store, store, REQUIREMENT)
    assert not store.pulls


def test_native_single_platform_images_need_no_new_docker_api():
    store = ImageStore({"Id": "native", "Os": "linux", "Architecture": "amd64"})
    assert inspect_base_image(store, store, REQUIREMENT)["Id"] == "native"
    assert not store.pulls
