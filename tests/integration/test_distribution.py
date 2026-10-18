"""Real compiler/dpkg/worker checks with a tiny fixture, never PostgreSQL DB tests."""

import os
import signal
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_build_cancellation import prepared_build

from pg_maker.artifacts.manifest import verify_manifest
from pg_maker.errors import PreconditionError
from pg_maker.packaging.deb import inspect_deb
from pg_maker.planning.runtime_repository import verify_repository
from pg_maker.storage import read_json

pytestmark = pytest.mark.docker

CONFIGURE = r"""#!/bin/sh
set -eu
mkdir -p src
echo 'enable_tap_tests = yes' > src/Makefile.global
for arg do
    case "$arg" in --prefix=*) prefix=${arg#--prefix=} ;; esac
done
printf 'prefix := %s\n' "$prefix" > Makefile
cat > pq.c <<'C'
int fixture(void) { return 0; }
C
cat > main.c <<'C'
#include <stdio.h>
extern int fixture(void);
int main(void) { puts("PostgreSQL 18.1"); return fixture(); }
C
printf '#!/bin/sh\nprefix=%s\n' "$prefix" > pg_config
cat >> pg_config <<'SH'
for arg do
    case "$arg" in
        --version) echo 'PostgreSQL 18.1' ;;
        --bindir) echo "$prefix/bin" ;;
        --libdir) echo "$prefix/lib" ;;
        --pkglibdir) echo "$prefix/lib/postgresql" ;;
        --sharedir) echo "$prefix/share/postgresql" ;;
        --sysconfdir) echo "$prefix/etc" ;;
        --docdir) echo "$prefix/share/doc/postgresql" ;;
        --includedir) echo "$prefix/include" ;;
        --pgxs) echo "$prefix/lib/postgresql/pgxs/src/makefiles/pgxs.mk" ;;
        *) exit 1 ;;
    esac
done
SH
chmod 755 pg_config
cat >> Makefile <<'MAKE'
.PHONY: all install world-bin install-world-bin check check-world
all world-bin:
	cc -shared -fPIC -Wl,-soname,libpq.so.5 -o libpq.so.5.0 pq.c
	cc -o postgres main.c ./libpq.so.5.0 -Wl,-rpath,$(prefix)/lib
install install-world-bin:
	mkdir -p $(prefix)/bin $(prefix)/lib $(prefix)/include $(prefix)/lib/postgresql/pgxs
	cp postgres $(prefix)/bin/postgres
	cp postgres $(prefix)/bin/psql
	cp postgres $(prefix)/bin/pg_ctl
	cp pg_config $(prefix)/bin/pg_config
	cp libpq.so.5.0 $(prefix)/lib/
	ln -sf libpq.so.5.0 $(prefix)/lib/libpq.so.5
	ln -sf libpq.so.5 $(prefix)/lib/libpq.so
	touch $(prefix)/include/header.h $(prefix)/lib/libpq.a
	touch $(prefix)/lib/postgresql/pgxs/excluded.mk
check check-world:
	@echo 'DB test must not run' >&2
	exit 99
MAKE
"""


def wait(service, state):
    directory = service.directory / state["operation_id"]
    result = service.wait(directory, timeout_seconds=60)
    assert result["status"] == "succeeded", result
    assert not result["resources"]
    return directory, result


def package_fixture(tmp_path, configure=CONFIGURE, **kwargs):
    return prepared_build(
        tmp_path, configure, source_files={"COPYRIGHT": "Fixture license\n"}, **kwargs
    )


def test_package_payload_cache_and_portable_export(tmp_path):
    service = package_fixture(tmp_path)
    directory, result = wait(service, service.build(tmp_path / "lock.json", goal="package"))
    assert result["result"]["db_tests"] == "skipped"
    assert result["result"]["qualification"] == "unverified"
    output = Path(result["result"]["reports"][0]["path"]).parent
    repository = read_json(output / "runtime-repository.json")
    verify_repository(output / "runtime-repository", repository)
    packages = read_json(output / "packages.json")
    by_role = {p["role"]: p for p in packages}
    delivered = set()
    for package in packages:
        inspected = inspect_deb(output / package["path"])
        files = {e["path"] for e in inspected["entries"] if e["type"] != "directory"}
        assert not files & delivered
        delivered |= files
        assert not any("/include/" in p or "/pgxs/" in p or p.endswith(".a") for p in files)
        assert "Suggests" not in inspected["control"]
        assert ("postinst" in inspected["scripts"]) == (package["role"] == "server")
        if package["role"] != "libpq":
            libpq = by_role["libpq"]
            assert f"{libpq['name']} (= {libpq['deb_version']})" in inspected["control"]["Depends"]
    manifest = read_json(output / "artifact-manifest.json")
    verify_manifest(output, manifest)
    assert manifest["publication_eligible"] is False
    exported = tmp_path / "exported"
    service.export(directory, exported, include_inputs=True)
    moved = tmp_path / "moved"
    exported.rename(moved)
    exported_output = moved / "output" / output.name
    verify_manifest(exported_output, read_json(exported_output / "artifact-manifest.json"))
    verify_repository(exported_output / "runtime-repository", repository)
    for record in (exported_output / "commands").glob("*.json"):
        for key in ("stdout_log", "stderr_log"):
            assert (exported_output / read_json(record)["result"][key]).is_file()
    assert (moved / "inputs").is_dir()
    second, second_result = wait(service, service.build(tmp_path / "lock.json", goal="package"))
    assert '"type":"cache_reused"' in (second / "events.jsonl").read_text()
    report = read_json(Path(second_result["result"]["reports"][0]["path"]))
    assert report["cache_producer"]["run_id"] == directory.name
    # The new package must name its own producer while retaining cached-build provenance.
    manifest2 = read_json(
        Path(second_result["result"]["reports"][0]["path"]).parent / "artifact-manifest.json"
    )
    assert manifest2["artifacts"][0]["producer"]["run_id"] == second.name
    assert manifest2["artifacts"][0]["build_producer"]["run_id"] == directory.name
    cache = service.directory / ".cache"
    blob = next((cache / "files").iterdir())
    blob.write_bytes(b"corrupted cached bytes")
    third, _ = wait(service, service.build(tmp_path / "lock.json", goal="package"))
    assert '"type":"cache_reused"' not in (third / "events.jsonl").read_text()
    assert "Cache invalid; rebuilding" in (third / "worker.log").read_text()


def test_resume_after_worker_loss_reuses_verified_compile(tmp_path):
    configure = CONFIGURE.replace(
        "install install-world-bin:\n",
        "install install-world-bin:\n\t@test -e resumed || (touch resumed; sleep 8)\n",
    )
    service = package_fixture(tmp_path, configure)
    started = service.build(tmp_path / "lock.json", goal="package", use_cache=False)
    directory = service.directory / started["operation_id"]
    try:
        deadline = time.monotonic() + 30
        marker = directory / "workspace/build/my-feature.debian13-x64.candidate/obj/resumed"
        while not marker.is_file():
            assert service.status(directory)["status"] in {"planned", "running"}
            assert time.monotonic() < deadline
            time.sleep(0.05)
        os.kill(started["owner"]["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while service.status(directory)["status"] != "interrupted":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        plan_hash = read_json(directory / "input.json")["plan"]["plan_hash"]
        with pytest.raises(PreconditionError):
            service.resume(directory, plan_hash="sha256:" + "0" * 64)
        resumed = service.resume(directory, plan_hash=plan_hash)
        assert resumed["attempt"] == 2
        # Retry starts from the compiled snapshot, so its own install waits once again.
        _, result = wait(service, resumed)
        assert result["attempt"] == 2
        events = (directory / "events.jsonl").read_text()
        assert '"type":"checkpoint_restored"' in events
        assert (directory / "attempts/1/logs").is_dir()
        assert (directory / "attempts/2/logs").is_dir()
        assert "attempt=2" in (directory / "attempts/2/progress.log").read_text()
        assert list((directory / "attempts/2/previous").glob("*/build-*"))
        with pytest.raises(PreconditionError):
            service.resume(directory, plan_hash=plan_hash)
    finally:
        service.cancel(directory)
        service.wait(directory, timeout_seconds=30)


def test_partial_matrix_resume_keeps_successful_branch(tmp_path):
    configure = CONFIGURE.replace(
        "printf 'prefix :=",
        'case "$prefix" in *other*) test -e /work/source/retry-allowed ;; esac\nprintf \'prefix :=',
    )
    service = package_fixture(tmp_path, configure, extra_feature=True)
    started = service.build(tmp_path / "lock.json", goal="package", use_cache=False)
    directory = service.directory / started["operation_id"]
    result = service.wait(directory, timeout_seconds=60)
    assert result["status"] == "partial", result
    assert len(result["result"]["reports"]) == 2
    original = (
        directory / "workspace/output/my-feature.debian13-x64.candidate/artifact-manifest.json"
    )
    contents = original.read_bytes()
    (directory / "workspace/source/retry-allowed").touch()
    plan_hash = read_json(directory / "input.json")["plan"]["plan_hash"]
    _, resumed = wait(service, service.resume(directory, plan_hash=plan_hash))
    assert resumed["attempt"] == 2
    assert original.read_bytes() == contents
    report = read_json(original.parent / "report.json")
    assert report["status"] == "succeeded"


def test_packages_install_and_remove_without_creating_database(tmp_path):
    manifest = os.environ.get("PG_MAKER_PACKAGE_ENVIRONMENTS")
    if not manifest:
        pytest.skip("set PG_MAKER_PACKAGE_ENVIRONMENTS to test offline package installation")
    service = package_fixture(tmp_path)
    _, result = wait(service, service.build(tmp_path / "lock.json", goal="package"))
    validate_installation(
        tmp_path,
        Path(result["result"]["reports"][0]["path"]).parent,
        read_json(tmp_path / "lock.json"),
        read_json(Path(manifest)),
        tmp_path / "inputs",
    )


def validate_installation(tmp_path, output, lock, environments, inputs):
    """Exercise the real isolated package controller with DB execution explicitly disabled."""
    from pg_maker.models import CleanupPolicy, EnvironmentSpec
    from pg_maker.runners.docker import DockerRunner, current_user
    from pg_maker.sources.store import InputStore
    from pg_maker.validation.packages import validate_packages

    config = lock["spec"]["config"]
    variant = config["variants"][0]
    images = {i["role"]: i for i in environments["spec"]["images"]}
    lock["spec"]["environments"] = {"spec": environments["spec"]}
    directory = tmp_path / ("installation-" + uuid.uuid4().hex[:8])
    directory.mkdir()
    state = {"attempt": 1, "owner_token": uuid.uuid4().hex, "resources": []}
    context = SimpleNamespace(
        directory=directory,
        state=state,
        check=lambda: None,
        token=SimpleNamespace(requested=lambda: False),
        on_process=lambda identity: None,
        event=lambda name, **kw: state.update(kw),
    )
    runner = DockerRunner(directory, operation_id=directory.name, owner_token=state["owner_token"])
    branch = SimpleNamespace(
        context=context,
        runner=runner,
        request=SimpleNamespace(lock=lock),
        config=config,
        variant=variant,
        branch="candidate",
        output=output,
        feature=config["features"][0],
        inputs=InputStore(inputs),
        report={},
        id=variant["id"] + ".candidate",
        handle=None,
        step={
            "image_id": images["test"]["image_id"],
            "resources": {"memory_bytes": 1024**3, "cpu_jobs": 2},
            "timeout_seconds": 180,
        },
    )

    def prepare():
        if branch.handle is None:
            branch.handle = runner.prepare(
                EnvironmentSpec(images["build"]["image_id"], current_user(), "repository")
            )

    def release():
        if branch.handle:
            runner.release(branch.handle, CleanupPolicy.REMOVE)
            branch.handle = None

    branch.prepare, branch.release = prepare, release
    try:
        report = validate_packages(branch, run_database=False)
        assert report["passed"] and report["run_database"] is False
        assert {s["set"] for s in report["sets"]} == {"server", "client", "libpq"}
        assert not state["resources"]
        commands = [
            read_json(p)["argv"]
            for p in (output / "package-tests").glob("*/*.json")
            if p.name != "result.json"
        ]
        executables = [
            argv[argv.index("--") + 1] if argv[0] == "runuser" else argv[0] for argv in commands
        ]
        assert not any(program.endswith("/initdb") for program in executables)
    finally:
        release()
