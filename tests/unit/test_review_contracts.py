"""Review regressions at package/configuration/acceptance boundaries."""

import pytest

from pg_maker.builders.autotools import verify_configure
from pg_maker.builders.components import sqlstate
from pg_maker.errors import CommandError, PreconditionError
from pg_maker.packaging.deb import declared_dependencies, verify_libpq_soname


def test_depends_uses_actual_versions_and_explicit_service_tools():
    actual = {"libc6 (>= 2.38)", "private-libpq (= 18.1)", "libssl3t64 (>= 3.0)"}
    packages, unused = declared_dependencies(
        actual, ["libc6", "libssl3t64", "libicu76"], ["coreutils", "passwd"], ["private-libpq"]
    )
    assert packages == sorted(actual | {"coreutils", "passwd"})
    assert unused == ["libicu76"]
    with pytest.raises(PreconditionError, match="undeclared") as raised:
        declared_dependencies(actual, ["libc6"], [], ["private-libpq"])
    assert raised.value.details["missing"] == ["libssl3t64"]


def test_libpq_soname_must_match_identity():
    verify_libpq_soname({"libpq.so.5"}, "5")
    with pytest.raises(PreconditionError, match="SONAME"):
        verify_libpq_soname({"libpq.so.6"}, "5")


def test_configure_rejects_unknown_and_unfulfilled_options():
    argv = ["./configure", "--prefix=/opt/example", "--with-icu", "--with-lz4"]
    settings = "prefix := /opt/example\nenable_tap_tests = yes\nwith_icu = yes\n"
    assert verify_configure(argv, settings, "#define USE_LZ4 1\n", "")["passed"]
    with pytest.raises(PreconditionError, match="unrecognized"):
        verify_configure(
            argv, settings, "#define USE_LZ4 1\n", "configure: WARNING: unrecognized options: --bad"
        )
    with pytest.raises(PreconditionError, match="differs"):
        verify_configure(argv, settings, "", "")
    with pytest.raises(PreconditionError, match="differ"):
        verify_configure(
            argv, settings.replace("with_icu = yes", "with_icu = no"), "#define USE_LZ4 1\n", ""
        )


def test_sqlstate_never_turns_connection_failure_into_expected_sql_error():
    assert sqlstate(0, "") == "00000"
    assert sqlstate(3, "psql:/tmp/probe.sql:1: ERROR:  22012: division by zero\n") == "22012"
    for code, text in [(2, "connection refused"), (3, "unstructured failure")]:
        with pytest.raises(CommandError):
            sqlstate(code, text)


def test_default_socket_probe_has_no_host_override_and_recreates_directory(tmp_path, monkeypatch):
    from pg_maker.validation import installed

    directory = tmp_path / "socket"
    directory.mkdir()
    harness = installed.Harness.__new__(installed.Harness)
    harness.service = {"socket": "/run/postgresql", "port": "5432", "user": "postgres"}
    harness.env = {"PGHOST": "wrong", "PGHOSTADDR": "127.0.0.1"}
    harness.bin = "/opt/test/bin/"
    actions = []

    def run(argv, **kwargs):
        assert "-h" not in argv
        assert "PGHOST" not in harness.env and "PGHOSTADDR" not in harness.env
        assert directory.is_dir()
        return "/run/postgresql"

    def service(action):
        actions.append(action)
        if action == "start":
            assert not directory.exists()
            directory.mkdir()

    def check(name, assertion):
        assert assertion

    harness.run, harness.systemctl, harness.check = run, service, check
    monkeypatch.setattr(installed, "Path", lambda path: directory)
    harness.default_socket()
    assert actions == ["stop", "start"]
