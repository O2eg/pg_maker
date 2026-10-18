"""Package ownership, metadata and evidence boundaries without building a database."""

import copy
import io
import os
import shutil
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from pg_maker.artifacts.manifest import finish
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.packaging.deb import inspect_deb
from pg_maker.packaging.spec import deb_control, field, package_spec
from pg_maker.packaging.tree import excluded, owner, seal_tree, split, verify_tree
from pg_maker.validation import systemctl


def test_failed_baseline_blocks_candidate_publication_before_any_mutation(tmp_path):
    from pg_maker.artifacts.manifest import complete_matrix
    from pg_maker.storage import content_hash, file_hash, read_json, write_json

    reports = []
    for branch in ("candidate", "baseline"):
        output = tmp_path / branch
        output.mkdir()
        (output / "fixture.deb").write_bytes(branch.encode())
        verification = {"evidence": [], "qualification": "package-verified"}
        verification["verification_hash"] = content_hash(verification)
        write_json(output / "verification.json", verification)
        manifest = {
            "artifacts": [
                {
                    "path": "fixture.deb",
                    "sha256": file_hash(output / "fixture.deb"),
                    "verification": {
                        "path": "verification.json",
                        "sha256": file_hash(output / "verification.json"),
                    },
                }
            ],
            "publication_eligible": False,
        }
        manifest["manifest_hash"] = content_hash(manifest)
        write_json(output / "artifact-manifest.json", manifest)
        report = output / "report.json"
        write_json(
            report,
            {
                "branch": branch,
                "status": "succeeded" if branch == "candidate" else "failed",
                "qualification": "package-verified",
                "publication_eligible": False,
            },
        )
        reports.append(report)
    with pytest.raises(PreconditionError, match="every verified branch"):
        complete_matrix(reports)
    assert read_json(reports[0])["publication_eligible"] is False
    assert read_json(reports[0].parent / "artifact-manifest.json")["publication_eligible"] is False
    write_json(reports[1], {**read_json(reports[1]), "status": "succeeded"})
    complete_matrix(reports)
    assert read_json(reports[0])["publication_eligible"] is True
    assert read_json(reports[1])["publication_eligible"] is False


@pytest.mark.parametrize(
    "path,role",
    [
        ("lib/libpq.so.5.18", "libpq"),
        ("share/locale/ru/LC_MESSAGES/libpq-18.mo", "libpq"),
        ("bin/psql", "client"),
        ("bin/pg_config", "client"),
        ("share/postgresql/psqlrc.sample", "client"),
        ("share/man/man1/pg_dump.1", "client"),
        ("bin/pg_ctl", "server"),
        ("lib/postgresql/hstore.so", "server"),
    ],
)
def test_role_is_independent_of_vendor_and_version(path, role):
    assert owner(path) == role


@pytest.mark.parametrize(
    "path",
    [
        "include/postgresql/server/postgres.h",
        "lib/postgresql/pgxs/src/Makefile.global",
        "bin/ecpg",
        "lib/libecpg.so.6",
        "lib/libpgtypes.so.3",
        "lib/libpq.a",
        "lib/libpq.so",
    ],
)
def test_development_files_are_excluded(path):
    assert excluded(path)


def test_tree_seal_detects_content_modes_links_and_overlapping_payload(tmp_path):
    root = tmp_path / "tree"
    (root / "opt/vendor/bin").mkdir(parents=True)
    (root / "opt/vendor/lib").mkdir()
    binary = root / "opt/vendor/bin/psql"
    library = root / "opt/vendor/lib/libpq.so.5.0"
    binary.write_text("binary")
    binary.chmod(0o755)
    library.write_text("library")
    (root / "opt/vendor/lib/libpq.so.5").symlink_to("libpq.so.5.0")
    roles = {
        "opt/vendor/bin/psql": "client",
        "opt/vendor/lib/libpq.so.5.0": "libpq",
        "opt/vendor/lib/libpq.so.5": "libpq",
    }
    doc = seal_tree(root, roles)
    verify_tree(root, doc)
    packages = [{"role": r, "name": "vendor-" + r} for r in ("server", "client", "libpq")]
    outputs = split(root, doc, tmp_path / "packages", packages)
    assert (outputs["client"] / "opt/vendor/bin/psql").is_file()
    assert not (outputs["server"] / "opt/vendor/bin/psql").exists()
    binary.chmod(0o644)
    with pytest.raises(PreconditionError):
        verify_tree(root, doc)
    binary.chmod(0o755)
    library.write_text("tampered")
    with pytest.raises(PreconditionError):
        verify_tree(root, doc)
    with pytest.raises(PreconditionError, match="no package owner"):
        seal_tree(root, {})


@pytest.mark.parametrize(
    "vendor,arch,version", [("alpha", "amd64", "18.1.0-1"), ("beta", "arm64", "19.2.4-7")]
)
def test_common_package_spec_drives_one_control_template(vendor, arch, version):
    config = {
        "vendor": {
            "description": "Private database",
            "company": vendor,
            "contact_email": "db@example.org",
            "homepage": None,
        }
    }
    variant = {
        "identity": {"service": vendor + "-service", "paths": {"runtime_prefix": "/opt/" + vendor}}
    }
    identity = {
        "role": "client",
        "name": vendor + "-client",
        "deb_version": version,
        "rpm_version": version.split("-")[0],
        "rpm_release": version.split("-")[1],
        "native_arch": arch,
        "filename": "fixture.deb",
    }
    tree = {"tree_hash": "fixture", "entries": [{"path": "psql", "role": "client"}]}
    spec = package_spec(config, variant, identity, [vendor + "-libpq (= " + version + ")"], tree)
    rendered, fields = deb_control(spec)
    assert fields["Package"] == vendor + "-client"
    assert fields["Architecture"] == arch
    assert fields["Version"] == version
    assert spec["identity"]["rpm_release"] == version.split("-")[1]
    assert spec["service"] is None and spec["files"] == tree["entries"]
    assert "Suggests:" not in rendered and "Homepage:" not in rendered
    assert "${" not in rendered
    assert fields["Maintainer"] == vendor + " <db@example.org>"
    assert fields["Description"] == "Private database PostgreSQL client"
    assert rendered == "".join(key + ": " + value + "\n" for key, value in fields.items()) + "\n"


@pytest.mark.parametrize("value", ["", "one\nDepends: injected", "one\r", "\0"])
def test_control_injection_rejected(value):
    with pytest.raises(ValidationError):
        field(value)


def deb_bytes(*, uid=0, member_path="opt/vendor/file", special=False):
    parts = {"debian-binary": b"2.0\n"}
    for name in ("control.tar.xz", "data.tar.xz"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:xz") as tar:
            entry = tarfile.TarInfo("control" if name.startswith("control") else member_path)
            entry.mode = 0o644
            data = b"Package: fixture\nVersion: 1\n" if name.startswith("control") else b"payload"
            entry.size = len(data)
            if name.startswith("data"):
                entry.uid = uid
                if special:
                    entry.type, entry.size = tarfile.FIFOTYPE, 0
            tar.addfile(entry, io.BytesIO(data))
        parts[name] = stream.getvalue()
    result = bytearray(b"!<arch>\n")
    for name, data in parts.items():
        result.extend(
            f"{name + '/':<16}{1:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        )
        result.extend(data)
        if len(data) % 2:
            result.extend(b"\n")
    return result


@pytest.mark.parametrize("kwargs", [{"uid": 1000}, {"member_path": "../escape"}, {"special": True}])
def test_deb_parser_rejects_wrong_ownership_paths_and_special_files(tmp_path, kwargs):
    path = tmp_path / "fixture.deb"
    path.write_bytes(deb_bytes())
    assert inspect_deb(path)["control"]["Package"] == "fixture"
    path.write_bytes(deb_bytes(**kwargs))
    with pytest.raises(PreconditionError):
        inspect_deb(path)
    path.write_bytes(b"!<arch>\ntruncated")
    with pytest.raises(PreconditionError):
        inspect_deb(path)


def test_skipped_installed_database_tests_never_qualify():
    branch = SimpleNamespace(
        report={
            "qualification": "source-verified",
            "package_tests": {"passed": True, "run_database": False},
        },
        request=SimpleNamespace(plan={"spec": {"goal": "verified"}}),
    )
    with pytest.raises(PreconditionError, match="lacks"):
        finish(branch)


def test_systemctl_pid_checks_real_executable_owner_and_liveness(tmp_path):
    import pwd

    binary = tmp_path / "postgres"
    shutil.copyfile("/bin/sh", binary)
    binary.chmod(0o755)
    pidfile = tmp_path / "postmaster.pid"
    service = {
        "PIDFile": str(pidfile),
        "User": pwd.getpwuid(os.getuid()).pw_name,
        "ExecStart": str(tmp_path / "pg_ctl") + " start",
    }
    child = subprocess.Popen(
        [str(binary), "-c", "read value", "-D", str(tmp_path)], stdin=subprocess.PIPE
    )
    try:
        pidfile.write_text(str(child.pid) + "\n" + str(tmp_path) + "\n")
        assert systemctl.pid(service) == child.pid
        other = copy.deepcopy(service)
        other["ExecStart"] = "/wrong/pg_ctl start"
        assert systemctl.pid(other) is None
        pidfile.write_text("1\n")
        assert systemctl.pid(service) is None
        pidfile.write_text(str(child.pid) + "\n/wrong-cluster\n")
        assert systemctl.pid(service) is None
        pidfile.write_text(str(child.pid) + "\n" + str(tmp_path) + "\n")
    finally:
        child.terminate()
        child.wait()
    assert systemctl.pid(service) is None


def test_systemctl_does_not_equate_zero_exit_to_readiness(monkeypatch):
    monkeypatch.setattr(systemctl, "load_unit", lambda name: {})
    monkeypatch.setattr(systemctl, "pid", lambda service: None)
    commands = []
    monkeypatch.setattr(systemctl, "execute", lambda service, key: commands.append(key))
    with pytest.raises(RuntimeError, match="without a running server"):
        systemctl.main(["start", "fixture.service"])
    assert commands == ["ExecStartPre", "ExecStart"]
    assert systemctl.main(["is-active", "fixture.service"]) == 3
    with pytest.raises(RuntimeError, match="inactive"):
        systemctl.main(["reload", "fixture.service"])
    with pytest.raises(ValueError, match="unsupported"):
        systemctl.main(["enable-now", "fixture.service"])
