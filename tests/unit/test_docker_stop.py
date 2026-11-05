from types import SimpleNamespace

import pytest

from pg_maker.errors import CommandError
from pg_maker.runners.docker import DockerRunner


@pytest.mark.parametrize("still_running", [False, True])
def test_stop_uses_verified_container_state_after_client_timeout(still_running):
    states = iter([{"id": "owned", "running": True}, {"id": "owned", "running": still_running}])
    runner = SimpleNamespace(
        _inspect=lambda handle: next(states),
        _control=lambda *a, **kw: SimpleNamespace(returncode=143, timed_out=True),
    )
    if still_running:
        with pytest.raises(CommandError, match="remained running"):
            DockerRunner.cancel(runner, object())
    else:
        DockerRunner.cancel(runner, object())
