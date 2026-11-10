"""Publication gates, native-repository protocols and durable CI worker behavior."""

import copy
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import threading
import time
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import yaml

from pg_maker.api import MakerService
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.publishing.artifacts import packages, plan
from pg_maker.publishing.config import load, validate
from pg_maker.publishing.receivers import Directory, Nexus
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, file_hash, read_json, write_json


def seal(root):
    manifest = {
        "qualification": "extension-package-verified",
        "evidence": [
            {"path": str(p.relative_to(root)), "sha256": file_hash(p)}
            for p in sorted(root.rglob("*"))
            if p.is_file() and p.name != "extension-manifest.json"
        ],
    }
    manifest["manifest_hash"] = content_hash(manifest)
    write_json(root / "extension-manifest.json", manifest)


def seal_export(root):
    contents = inventory(root)
    contents["entries"] = [e for e in contents["entries"] if e["path"] != "export-manifest.json"]
    write_json(
        root / "export-manifest.json",
        {"operation_id": "build", "inventory": contents, "tree_hash": content_hash(contents)},
    )


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "export"
    output = root / "output"
    (output / "packages").mkdir(parents=True)
    payload = {"operation_kind": "extension_build"}
    write_json(root / "input.json", payload)
    write_json(
        root / "state.json",
        {
            "operation_id": "build",
            "input_hash": content_hash(payload),
            "operation_kind": "extension_build",
            "status": "succeeded",
            "resources": [],
        },
    )
    name = "postgresql-18-pg-repack_1.5.3-1.deb13_amd64.deb"
    path = output / "packages" / name
    path.write_bytes(b"package bytes qualified by fixture")
    record = {
        "path": name,
        "sha256": file_hash(path),
        "name": "postgresql-18-pg-repack",
        "version": "1.5.3-1.deb13",
        "architecture": "amd64",
    }
    tests = {"passed": True, "installed_inventory": True}
    write_json(output / "package-tests/package-tests.json", tests)
    write_json(
        output / "report.json",
        {
            "qualification": "extension-package-verified",
            "packages": [record],
            "package_tests": tests,
            "target": {"server_package": {"architecture": "amd64"}},
        },
    )
    seal(output)
    seal_export(root)
    return root


def directory_config(path):
    return validate(
        {
            "repositories": [
                {
                    "id": "local",
                    "adapter": "directory",
                    "formats": ["deb", "rpm"],
                    "directory": str(path),
                }
            ]
        }
    )


def write_config(path, config):
    path.write_text(
        yaml.safe_dump(
            {"api_version": "pg_maker/publication/v1", "kind": "PublicationConfig", "spec": config}
        )
    )
    return path


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["repositories"][0].update(password="secret"),
        lambda c: c["repositories"][0].update(path_prefix="../escape"),
        lambda c: c["repositories"].append(copy.deepcopy(c["repositories"][0])),
        lambda c: c["repositories"][0].update(url="https://example.org"),
    ],
)
def test_strict_receiver_config(tmp_path, mutation):
    config = directory_config(tmp_path / "destination")
    mutation(config)
    with pytest.raises(ValidationError):
        validate(config)


def test_relative_paths_and_secret_references(tmp_path):
    config = {
        "repositories": [
            {
                "id": "nexus",
                "adapter": "nexus-apt",
                "url": "https://example.org/nexus/",
                "repository": "apt",
                "formats": ["deb"],
                "suite": "deb",
                "component": "main",
                "auth": {"netrc_file": "secrets/netrc"},
                "tls": {"ca_file": "ca.pem"},
            }
        ]
    }
    value = load(write_config(tmp_path / "publish.yaml", config))
    receiver = value["repositories"][0]
    assert receiver["url"] == "https://example.org/nexus"
    assert receiver["auth"]["netrc_file"] == str(tmp_path / "secrets/netrc")
    assert receiver["tls"]["ca_file"] == str(tmp_path / "ca.pem")
    receiver["url"] = "https://name:password@example.org"
    with pytest.raises(ValidationError):
        validate(value)


def test_unverified_changed_and_incomplete_sources_blocked(source, tmp_path):
    config = directory_config(tmp_path / "destination")
    assert len(plan(source, config)["destinations"]) == 1
    target = source / "output/packages"
    p = next(target.iterdir())
    p.write_bytes(b"changed")
    with pytest.raises(PreconditionError):
        plan(source, config)
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("change", ["tests", "state", "resources", "symlink", "evidence"])
def test_publication_requires_successful_installed_tests(source, change):
    if change in ("state", "resources"):
        state = read_json(source / "state.json")
        state["status"] = "partial" if change == "state" else "succeeded"
        state["resources"] = [] if change == "state" else [{"kind": "docker"}]
        write_json(source / "state.json", state)
    elif change == "symlink":
        path = next((source / "output/packages").iterdir())
        original = path.read_bytes()
        path.unlink()
        (source / "outside").write_bytes(original)
        path.symlink_to(source / "outside")
    else:
        report = read_json(source / "output/report.json")
        report["package_tests"]["passed"] = False
        write_json(source / "output/report.json", report)
        if change == "tests":
            write_json(source / "output/package-tests/package-tests.json", report["package_tests"])
            seal(source / "output")
            seal_export(source)
    with pytest.raises(PreconditionError):
        packages(source)


@pytest.fixture
def core_source(source):
    shutil.rmtree(source / "output")
    payload = {
        "operation_kind": "build",
        "plan": {
            "spec": {
                "steps": [
                    {
                        "kind": "export",
                        "outputs": [{"root": "output", "path": branch + "/report.json"}],
                    }
                    for branch in ("candidate", "baseline")
                ]
            }
        },
    }
    write_json(source / "input.json", payload)
    write_json(
        source / "state.json",
        {
            "operation_id": "build",
            "input_hash": content_hash(payload),
            "operation_kind": "build",
            "status": "succeeded",
            "resources": [],
        },
    )
    for branch in ("candidate", "baseline"):
        out = source / "output" / branch
        out.mkdir(parents=True)
        pkg = out / "pg.deb"
        pkg.write_bytes(branch.encode())
        write_json(out / "package-tests/server/result.json", {"passed": True})
        tests = {
            "passed": True,
            "run_database": True,
            "sets": [
                {
                    "set": "server",
                    "passed": True,
                    "path": "package-tests/server/result.json",
                    "sha256": file_hash(out / "package-tests/server/result.json"),
                }
            ],
        }
        write_json(out / "package-tests.json", tests)
        verification = {
            "qualification": "unverified",
            "db_tests": "skipped",
            "evidence": [
                {"path": str(p.relative_to(out)), "sha256": file_hash(p)}
                for p in out.rglob("*.json")
            ],
        }
        verification["verification_hash"] = content_hash(verification)
        write_json(out / "verification.json", verification)
        record = {
            "path": "pg.deb",
            "name": "pg",
            "deb_version": "18.6-1",
            "native_arch": "amd64",
            "role": "server" if branch == "candidate" else "baseline-package",
            "sha256": file_hash(pkg),
            "verification": {
                "path": "verification.json",
                "sha256": file_hash(out / "verification.json"),
            },
        }
        manifest = {"artifacts": [record], "publication_eligible": False}
        manifest["manifest_hash"] = content_hash(manifest)
        write_json(out / "artifact-manifest.json", manifest)
    seal_export(source)
    return source


def test_core_package_only_evidence_and_baseline(core_source):
    source = core_source
    assert len(packages(source)) == 1
    (source / "output/baseline/package-tests/server/result.json").write_text("{}")
    with pytest.raises(PreconditionError):
        packages(source)


@pytest.mark.parametrize("exported", [True, False])
@pytest.mark.parametrize("branch", ["baseline", "candidate"])
def test_publication_rejects_missing_build_branch(core_source, exported, branch):
    source = core_source
    if not exported:
        (source / "export-manifest.json").unlink()
        (source / "workspace").mkdir()
        (source / "output").rename(source / "workspace/output")
    assert len(packages(source)) == 1
    root = source / ("output" if exported else "workspace/output")
    shutil.rmtree(root / branch)
    with pytest.raises(PreconditionError, match="inventory|build plan"):
        packages(source)


def test_publication_rejects_incomplete_resealed_export(core_source):
    shutil.rmtree(core_source / "output/baseline")
    seal_export(core_source)
    with pytest.raises(PreconditionError, match="build plan"):
        packages(core_source)


@pytest.mark.parametrize("change", ["empty", "tree_hash", "seal", "extra"])
def test_publication_checks_export_inventory(source, change):
    manifest = read_json(source / "export-manifest.json")
    if change == "empty":
        write_json(source / "export-manifest.json", {})
    elif change == "tree_hash":
        manifest["tree_hash"] = "sha256:" + "0" * 64
        write_json(source / "export-manifest.json", manifest)
    elif change == "seal":
        with (source / "output/extension-manifest.json").open("a") as stream:
            stream.write("\n")
    else:
        (source / "output/extra.deb").write_bytes(b"untested")
    with pytest.raises(PreconditionError, match="export"):
        packages(source)


def test_publication_does_not_read_exported_sdk(source, monkeypatch):
    sdk = source / "inputs/sdk.tar"
    sdk.parent.mkdir()
    sdk.write_bytes(b"SDK is irrelevant to publication")
    seal_export(source)
    from pg_maker.publishing import artifacts

    original = artifacts.file_hash

    def checked(path):
        assert not path.is_relative_to(sdk.parent)
        return original(path)

    monkeypatch.setattr(artifacts, "file_hash", checked)
    assert len(packages(source)) == 1


def test_directory_idempotence_and_conflict(source, tmp_path):
    config = directory_config(tmp_path / "destination")
    receiver = Directory(config["repositories"][0], config, lambda: None)
    package = packages(source)[0]
    receiver.preflight([package])
    assert receiver.publish(package)["action"] == "uploaded"
    assert receiver.publish(package)["action"] == "reused"
    (tmp_path / "destination" / package["filename"]).write_bytes(b"other")
    with pytest.raises(PreconditionError):
        receiver.preflight([package])


class Server:
    def __init__(self, fmt, package):
        self.format = fmt
        self.package = package
        self.assets = {}
        self.uploads = 0
        self.commit_error = False
        self.hide_index = False
        self.bad_index = False
        self.foreign = False
        self.corrupt_download = False
        self.policy = "ALLOW_ONCE"
        self.auth = []
        self.read_failure = None
        self.server = None

    def handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200, content="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                failure = owner.read_failure
                if failure and self.path.endswith(failure[0]):
                    owner.read_failure = None
                    self.wfile.flush()
                    if failure[1] == "timeout":
                        time.sleep(1.2)
                    else:
                        self.wfile.write(body[:1])
                    self.close_connection = True
                    return
                self.wfile.write(body)

            def json(self, value):
                self.send(json.dumps(value).encode())

            def do_GET(self):
                owner.auth.append(self.headers.get("Authorization"))
                path = unquote(urlsplit(self.path).path)
                if path == "/service/rest/v1/repositories/repo":
                    return self.json({"format": owner.format, "type": "hosted"})
                if "/hosted/" in path:
                    return self.json(
                        {"storage": {"writePolicy": owner.policy}, "apt": {"distribution": "deb"}}
                    )
                if path == "/service/rest/v1/assets":
                    items = [
                        {
                            "path": name,
                            "checksum": {"sha256": hashlib.sha256(data).hexdigest()},
                            "downloadUrl": "https://foreign.invalid/secret"
                            if owner.foreign
                            else owner.url + "/repository/repo/" + name,
                        }
                        for name, data in owner.assets.items()
                    ]
                    return self.json({"items": items, "continuationToken": None})
                if path.endswith("/Packages.gz"):
                    p = owner.package
                    digest = "0" * 64 if owner.bad_index else p["sha256"][7:]
                    text = (
                        ""
                        if owner.hide_index
                        else "\n".join(
                            [
                                f"Package: {p['name']}",
                                f"Version: {p['version']}",
                                f"Architecture: {p['architecture']}",
                                f"SHA256: {digest}",
                                f"Size: {p['size_bytes']}",
                                f"Filename: pool/{p['filename']}",
                                "\n",
                            ]
                        )
                    )
                    return self.send(gzip.compress(text.encode()))
                if path.endswith("/repomd.xml"):
                    raw = owner.primary()
                    checksum = hashlib.sha256(raw).hexdigest()
                    return self.send(
                        (
                            '<repomd xmlns="http://linux.duke.edu/metadata/repo">'
                            '<data type="primary"><location href="repodata/primary.xml.gz"/>'
                            f'<checksum type="sha256">{checksum}</checksum></data></repomd>'
                        ).encode()
                    )
                if path.endswith("/primary.xml.gz"):
                    return self.send(owner.primary())
                prefix = "/repository/repo/"
                if path.startswith(prefix) and path[len(prefix) :] in owner.assets:
                    data = owner.assets[path[len(prefix) :]]
                    return self.send(
                        b"wrong" if owner.corrupt_download else data,
                        content="application/octet-stream",
                    )
                self.send(b"not found", 404)

            def do_POST(self):
                assert parse_qs(urlsplit(self.path).query)["repository"] == ["repo"]
                body = self.rfile.read(int(self.headers["Content-Length"]))
                message = BytesParser(policy=default).parsebytes(
                    (
                        "Content-Type: "
                        + self.headers["Content-Type"]
                        + "\r\nMIME-Version: 1.0\r\n\r\n"
                    ).encode()
                    + body
                )
                part = list(message.iter_parts())[0]
                assert part.get_param("name", header="content-disposition") == "apt.asset"
                owner.assets["pool/" + part.get_filename()] = part.get_payload(decode=True)
                owner.uploads += 1
                self.send(b"receiver-secret-do-not-log", 503 if owner.commit_error else 204)

            def do_PUT(self):
                name = unquote(urlsplit(self.path).path).removeprefix("/repository/repo/")
                if name in owner.assets:
                    return self.send(b"exists", 400)
                owner.assets[name] = self.rfile.read(int(self.headers["Content-Length"]))
                owner.uploads += 1
                self.send(b"", 503 if owner.commit_error else 201)

        return Handler

    def primary(self):
        p = self.package
        checksum = "0" * 64 if self.bad_index else p["sha256"][7:]
        body = (
            ""
            if self.hide_index
            else (
                f'<package><location href="{p["filename"]}"/>'
                f'<checksum type="sha256">{checksum}</checksum></package>'
            )
        )
        return gzip.compress(
            (
                '<metadata xmlns="http://linux.duke.edu/metadata/common">' + body + "</metadata>"
            ).encode(),
            mtime=0,
        )

    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def config(self):
        r = {
            "id": "nexus",
            "adapter": "nexus-" + self.format,
            "url": self.url,
            "repository": "repo",
            "formats": ["rpm" if self.format == "yum" else "deb"],
            "allow_http": True,
        }
        if self.format == "apt":
            r.update(suite="deb", component="main")
        return validate(
            {"repositories": [r], "verification_timeout_seconds": 1, "poll_interval_seconds": 1}
        )


@pytest.mark.parametrize("fmt", ["apt", "yum", "raw"])
def test_nexus_upload_download_index_and_idempotence(source, fmt):
    package = packages(source)[0]
    if fmt == "yum":
        package = {**package, "filename": "pg-repack-1.5.3-1.x86_64.rpm", "format": "rpm"}
    with Server(fmt, package) as server:
        config = server.config()
        receiver = Nexus(config["repositories"][0], config, lambda: None)
        receiver.preflight([package])
        receipt = receiver.publish(package)
        assert receipt["index_verified"] == (fmt != "raw")
        assert receipt["action"] == "uploaded"
        assert receiver.publish(package)["action"] == "reused" and server.uploads == 1
        server.assets[next(iter(server.assets))] = b"conflicting bytes"
        with pytest.raises(PreconditionError, match="different bytes"):
            receiver.preflight([package])


def test_committed_but_error_response_is_reconciled(source):
    package = packages(source)[0]
    with Server("apt", package) as server:
        server.commit_error = True
        config = server.config()
        receiver = Nexus(config["repositories"][0], config, lambda: None)
        assert receiver.publish(package)["action"] == "uploaded" and server.uploads == 1


@pytest.mark.parametrize("phase", ["metadata", "package"])
@pytest.mark.parametrize("failure", ["timeout", "truncated"])
def test_response_read_failure_reconciles_without_reupload(source, phase, failure):
    package = packages(source)[0]
    with Server("apt", package) as server:
        config = server.config()
        config.update(request_timeout_seconds=1, verification_timeout_seconds=5)
        server.read_failure = (
            "/Packages.gz" if phase == "metadata" else "/" + package["filename"],
            failure,
        )
        receiver = Nexus(config["repositories"][0], config, lambda: None)
        receipt = receiver.publish(package)
        assert receipt["index_verified"] and server.uploads == 1
        assert server.read_failure is None


def test_http_read_keeps_cancellation_and_size_limits(source):
    from pg_maker.errors import CancelledError

    package = packages(source)[0]
    with Server("raw", package) as server:
        config = server.config()
        receiver = Nexus(config["repositories"][0], config, lambda: None)
        url = server.url + "/service/rest/v1/assets"
        with pytest.raises(PreconditionError, match="size limit"):
            receiver.http.bytes(url, limit=1)

        def cancel():
            raise CancelledError("cancelled")

        with receiver.http.open(url) as response:
            receiver.http.check = cancel
            with pytest.raises(CancelledError):
                list(receiver.http.blocks(response, 65536))


@pytest.mark.parametrize("issue", ["hide_index", "bad_index", "corrupt_download", "policy"])
def test_nexus_rejects_unverified_remote_state(source, issue):
    package = packages(source)[0]
    with Server("apt", package) as server:
        setattr(server, issue, "ALLOW" if issue == "policy" else True)
        config = server.config()
        receiver = Nexus(config["repositories"][0], config, lambda: None)
        with pytest.raises(PreconditionError):
            receiver.preflight([package])
            receiver.publish(package)


def test_credentials_stay_runtime_only_and_foreign_url_rejected(source, monkeypatch):
    package = packages(source)[0]
    with Server("raw", package) as server:
        config = server.config()
        r = config["repositories"][0]
        r["auth"] = {"username_env": "CI_NEXUS_USER", "password_env": "CI_NEXUS_PASSWORD"}
        monkeypatch.setenv("CI_NEXUS_USER", "test")
        monkeypatch.setenv("CI_NEXUS_PASSWORD", "secret-value")
        receiver = Nexus(r, config, lambda: None)
        server.foreign = True
        receiver.publish(package)
        assert all(a and a.startswith("Basic ") for a in server.auth)
        with pytest.raises(PreconditionError, match="foreign"):
            receiver.http.open("http://foreign.invalid/")
        assert "secret-value" not in json.dumps(config)


def test_worker_resume_reconciles_upload_and_cli_request_id(source, tmp_path):
    package = packages(source)[0]
    with Server("apt", package) as server:
        server.hide_index = True
        config = write_config(tmp_path / "publish.yaml", server.config())
        service = MakerService(tmp_path / "operations")
        state = service.publish(source, config, request_id="ci-1")
        op = service.directory / state["operation_id"]
        failed = service.wait(op, timeout_seconds=30)
        assert failed["status"] == "failed" and server.uploads == 1
        server.hide_index = False
        service.resume(op, input_hash=failed["input_hash"])
        done = service.wait(op, timeout_seconds=30)
        assert done["status"] == "succeeded" and server.uploads == 1
        report = read_json(Path(done["result"]["document"]))
        assert report["artifacts"][0]["action"] == "reused"
        assert report["qualification"] == "publication-verified"
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "pg_maker",
                "--machine",
                "--request-id",
                "ci-1",
                "publish",
                "run",
                str(source),
                "--config",
                str(config),
                "--operations",
                str(service.directory),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert cli.returncode == 0, cli.stderr + cli.stdout
        assert json.loads(cli.stdout)["result"]["operation_id"] == state["operation_id"]
        assert len(list(service.directory.glob("*/state.json"))) == 1


def test_waiting_publication_can_cancel_and_resume(source, tmp_path):
    state = read_json(source / "state.json")
    state["status"] = "running"
    write_json(source / "state.json", state)
    config = write_config(tmp_path / "publish.yaml", directory_config(tmp_path / "destination"))
    service = MakerService(tmp_path / "operations")
    started = service.publish(source, config, wait_for_source=True)
    op = service.directory / started["operation_id"]
    service.cancel(op)
    cancelled = service.wait(op, timeout_seconds=15)
    assert cancelled["status"] == "cancelled"
    state["status"] = "succeeded"
    write_json(source / "state.json", state)
    seal_export(source)
    service.resume(op, input_hash=cancelled["input_hash"])
    assert service.wait(op, timeout_seconds=15)["status"] == "succeeded"


def replace_tested_package(source):
    report = read_json(source / "output/report.json")
    package = source / "output/packages" / report["packages"][0]["path"]
    package.write_bytes(b"another tested build using identical locked inputs")
    report["packages"][0]["sha256"] = file_hash(package)
    write_json(source / "output/report.json", report)
    seal(source / "output")
    seal_export(source)


@pytest.mark.parametrize("wait_for_source", [False, True])
def test_completed_retry_rejects_changed_artifacts(source, tmp_path, wait_for_source):
    config = directory_config(tmp_path / "destination")
    service = MakerService(tmp_path / "operations")
    first = service.publish(source, config, request_id="ci-build", wait_for_source=wait_for_source)
    done = service.wait(service.directory / first["operation_id"], timeout_seconds=15)
    assert done["status"] == "succeeded"
    same = service.publish(source, config, request_id="ci-build", wait_for_source=wait_for_source)
    assert same["operation_id"] == first["operation_id"] and same["status"] == "succeeded"
    replace_tested_package(source)
    with pytest.raises(PreconditionError, match="different inputs|source changed"):
        service.publish(source, config, request_id="ci-build", wait_for_source=wait_for_source)
    assert len(list(service.directory.glob("*/state.json"))) == 1


def test_worker_rejects_artifacts_changed_after_request(source, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pg_maker.publishing.execution import publish

    config = directory_config(tmp_path / "destination")
    service = MakerService(tmp_path / "operations")
    requests = []

    def start(request, **kwargs):
        requests.append(request)
        return {"status": "planned"}

    monkeypatch.setattr(service, "start_operation", start)
    service.publish(source, config)
    replace_tested_package(source)
    with pytest.raises(PreconditionError, match="after the request"):
        publish(requests[0], SimpleNamespace(check=lambda: None))
    assert not (tmp_path / "destination").exists()


def test_all_receivers_preflight_before_first_write(source, tmp_path):
    config = directory_config(tmp_path / "good")
    config["repositories"].append(
        {
            "id": "bad",
            "adapter": "directory",
            "formats": ["deb"],
            "directory": str(tmp_path / "bad"),
        }
    )
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / packages(source)[0]["filename"]).write_bytes(b"conflict")
    service = MakerService(tmp_path / "operations")
    start = service.publish(source, config)
    assert (
        service.wait(service.directory / start["operation_id"], timeout_seconds=15)["status"]
        == "failed"
    )
    assert not (tmp_path / "good").exists()


def test_automatic_build_schedules_independent_publisher(tmp_path, monkeypatch):
    from pg_maker.models import ExtensionRequest

    config = write_config(tmp_path / "pub.yaml", directory_config(tmp_path / "destination"))
    service = MakerService(tmp_path / "operations")
    calls = []
    monkeypatch.setattr(service, "start_operation", lambda *a, **kw: {"operation_id": "build"})
    monkeypatch.setattr(
        service,
        "publish",
        lambda *a, **kw: calls.append((a, kw)) or {"operation_id": "publication"},
    )
    request = ExtensionRequest(
        "extension_build", {"validation": {"run_tests": True}}, "/out", "/inputs"
    )
    result = service._start_build(
        request, operation_id="build", request_id="ci", publish_config=config
    )
    assert result["operation_id"] == "publication"
    assert calls[0][0][0] == service.directory / "build" and calls[0][1]["wait_for_source"]
    bad = ExtensionRequest(
        "extension_build", {"validation": {"run_tests": False}}, "/out", "/inputs"
    )
    with pytest.raises(PreconditionError):
        service._start_build(bad, operation_id="build", request_id="ci", publish_config=config)


def test_upload_uses_frozen_tested_bytes(source, monkeypatch):
    package = packages(source)[0]
    original = Path(package["path"]).read_bytes()
    with Server("raw", package) as server:
        config = server.config()
        receiver = Nexus(config["repositories"][0], config, lambda: None)
        open_request = receiver.http.open

        def mutate_before_request(*args, **kwargs):
            if kwargs.get("method") == "PUT":
                Path(package["path"]).write_bytes(b"new untested content")
            return open_request(*args, **kwargs)

        monkeypatch.setattr(receiver.http, "open", mutate_before_request)
        receiver.publish(package)
        assert list(server.assets.values()) == [original]


def test_directory_rejects_symlink_receiver(source, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    config = directory_config(link)
    with pytest.raises(PreconditionError, match="symlink"):
        Directory(config["repositories"][0], config, lambda: None)
