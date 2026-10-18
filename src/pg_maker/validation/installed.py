#!/usr/bin/python3
"""Standalone package lifecycle harness, executed only in an isolated test container."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path


class Harness:
    def __init__(self, config, output):
        self.config, self.output = config, Path(output)
        self.output.mkdir()
        self.results = []
        self.env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"}
        self.service = config["service"]
        self.bin = self.service["prefix"] + "/bin/"
        self.data = Path(self.service["data"])
        self.sequence = 0

    def run(self, argv, *, user=False, expected=0, timeout=180, env=None, accepted_exit_codes=None):
        self.sequence += 1
        if user:
            argv = ["runuser", "-u", self.service["user"], "-g", self.service["group"], "--", *argv]
        started = time.monotonic()
        stdout = self.output / (str(self.sequence) + ".stdout.log")
        stderr = self.output / (str(self.sequence) + ".stderr.log")
        with stdout.open("wb") as out, stderr.open("wb") as err:
            result = subprocess.run(
                argv,
                env={**self.env, **(env or {})},
                stdout=out,
                stderr=err,
                timeout=timeout,
                check=False,
            )
        record = {
            "argv": argv,
            "returncode": result.returncode,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "stdout": stdout.name,
            "stderr": stderr.name,
        }
        (self.output / (str(self.sequence) + ".json")).write_text(json.dumps(record))
        self.last_returncode = result.returncode
        if (
            result.returncode not in accepted_exit_codes
            if accepted_exit_codes is not None
            else (expected == 0 and result.returncode != 0)
            or (expected != 0 and result.returncode == 0)
        ):
            raise RuntimeError("unexpected command status: " + repr(record))
        return stdout.read_text(errors="replace").strip()

    def check(self, name, assertion=True, **details):
        if not assertion:
            raise RuntimeError("package check failed: " + name)
        self.results.append({"id": name, "status": "passed", **details})
        self.save(False)

    def save(self, passed, error=None):
        (self.output / "result.json").write_text(
            json.dumps(
                {
                    "passed": passed,
                    "checks": self.results,
                    "error": error,
                    "service_manager": "emulated",
                    "native_systemd": "not-tested",
                }
            )
        )

    def sql(self, query, *, database="postgres", tcp=False):
        return self.run(
            [
                self.bin + "psql",
                "-X",
                "-A",
                "-t",
                "-v",
                "ON_ERROR_STOP=1",
                "-h",
                "127.0.0.1" if tcp else self.service["socket"],
                "-p",
                self.service["port"],
                "-U",
                self.service["user"],
                "-d",
                database,
                "-c",
                query,
            ],
            user=True,
            env={"PGPASSWORD": "pg_maker_disposable_test"},
        )

    def systemctl(self, command, expected=0):
        return self.run(
            ["systemctl", command, self.service["name"] + ".service"], expected=expected
        )

    def default_socket(self):
        """No host option/environment and no socket setting in postgresql.conf."""
        self.env.pop("PGHOST", None)
        self.env.pop("PGHOSTADDR", None)
        argv = [
            self.bin + "psql",
            "-X",
            "-A",
            "-t",
            "-v",
            "ON_ERROR_STOP=1",
            "-p",
            self.service["port"],
            "-U",
            self.service["user"],
            "-d",
            "postgres",
            "-c",
            "SHOW unix_socket_directories",
        ]
        self.check("compiled-socket-default", self.run(argv, user=True) == self.service["socket"])
        if self.service["socket"] in {"/run/postgresql", "/var/run/postgresql"}:
            self.systemctl("stop")
            Path(self.service["socket"]).rmdir()
            self.systemctl("start")
            self.check(
                "socket-directory-recreated", self.run(argv, user=True) == self.service["socket"]
            )

    def installed_inventory(self):
        roles = self.config["roles"]
        for entry in self.config["inventory"]:
            if entry["role"] not in roles:
                continue
            path = Path("/") / entry["path"]
            mode = path.lstat()
            self.check(
                "file:" + entry["path"],
                stat.S_IMODE(mode.st_mode) == entry["mode"]
                and mode.st_uid == 0
                and mode.st_gid == 0,
            )
            if entry["type"] == "symlink":
                if not path.is_symlink() or os.readlink(path) != entry["target"]:
                    raise RuntimeError("installed symlink differs")
            else:
                with path.open("rb") as stream:
                    digest = hashlib.sha256()
                    magic = stream.read(4)
                    digest.update(magic)
                    for block in iter(lambda: stream.read(1048576), b""):
                        digest.update(block)
                if "sha256:" + digest.hexdigest() != entry["sha256"]:
                    raise RuntimeError("installed file hash differs")
                if magic == b"\x7fELF":
                    output = self.run(["ldd", str(path)])
                    if "not found" in output or "/work/" in output:
                        raise RuntimeError(
                            "installed ELF dependency is missing or uses the build tree"
                        )
        self.check("installed-inventory-and-loader")

    def documentation(self):
        docs = self.config.get("documentation")
        if not docs:
            return
        roles = self.config["roles"]
        if "docs" in roles:
            sys.path.insert(0, "/work/source/doc-tools")
            from pg_maker.documentation.validation import html_tree

            root = Path(self.config["documentation_root"])
            expected = (
                [docs["context"][key] for key in ("vendor.brand", "product.version", "feature.id")]
                if self.config["documentation_branding"] != "none"
                else []
            )
            for language in docs["languages"]:
                if "html" in docs["formats"]:
                    html_tree(root / language / "html", language=language, expected=expected)
                if "pdf" in docs["formats"]:
                    pdf = root / language / "pdf/manual.pdf"
                    self.run(["pdfinfo", str(pdf)])
                    text = self.run(["pdftotext", str(pdf), "-"])
                    if any(value not in text for value in expected):
                        raise RuntimeError("installed PDF lacks product metadata")
        pages = [
            entry for entry in docs["files"] if entry["role"] in roles and entry["format"] == "man"
        ]
        for entry in pages:
            path = Path("/") / entry["path"]
            text = path.read_text()
            if not text.strip() or not (".TH " in text or text.startswith(".so ")):
                raise RuntimeError("installed manpage is invalid")
        self.check("installed-documentation", offline=True, manpages=len(pages))

    def prepare(self):
        source = Path("/work/source")
        shutil.copytree(source / "repository", Path("/tmp/runtime-repository"))
        for path in Path("/tmp/runtime-repository").rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        Path("/tmp/runtime-repository").chmod(0o755)
        shutil.copytree(source / "packages", Path("/tmp/packages"))
        for path in Path("/tmp/packages").iterdir():
            path.chmod(0o644)
        Path("/tmp/packages").chmod(0o755)
        sources = Path("/tmp/pg-maker-sources.list")
        sources.write_text("deb [trusted=yes] file:/tmp/runtime-repository ./\n")
        Path("/tmp/pg-maker-apt-lists/partial").mkdir(parents=True)
        self.apt = [
            "apt-get",
            "-y",
            "--no-install-recommends",
            "-o",
            "Dir::Etc::sourcelist=" + str(sources),
            "-o",
            "Dir::Etc::sourceparts=-",
            "-o",
            "Dir::State::lists=/tmp/pg-maker-apt-lists",
        ]
        # Debian slim excludes /usr/share/doc, including component licenses.
        # Check the complete product payload even with those base-image filters.
        for package in self.config["packages"]:
            self.apt.extend(
                ["-o", "Dpkg::Options::=--path-include=/usr/share/doc/" + package["name"] + "/*"]
            )
        self.run([*self.apt, "update"])
        shutil.copyfile(source / "systemctl.py", Path("/usr/bin/systemctl"))
        Path("/usr/bin/systemctl").chmod(0o755)
        Path("/run/systemd/system").mkdir(parents=True, exist_ok=True)
        self.package_paths = [
            "/tmp/packages/" + Path(p["path"]).name
            for p in self.config["packages"]
            if p["role"] in self.config["roles"]
        ]
        self.package_names = [
            p["name"] for p in self.config["packages"] if p["role"] in self.config["roles"]
        ]

    def initialize(self, data):
        self.run(
            [
                "install",
                "-d",
                "-m",
                "0700",
                "-o",
                self.service["user"],
                "-g",
                self.service["group"],
                str(data),
            ]
        )
        self.run(
            [
                self.bin + "initdb",
                "-D",
                str(data),
                "--data-checksums",
                "--no-locale",
                "--encoding=UTF8",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "-U",
                self.service["user"],
            ],
            user=True,
        )

    def acceptance(self):
        seen = set()
        for test in self.config["acceptance"]:
            if test["id"] in seen:
                continue
            seen.add(test["id"])
            # Each acceptance test receives a fresh independent installed-product cluster.
            data = Path("/tmp/acceptance-" + test["id"])
            self.initialize(data)
            with (data / "postgresql.conf").open("a") as stream:
                stream.write(
                    "\nlisten_addresses = ''\nunix_socket_directories = '"
                    + str(data)
                    + "'\nport = 55439\n"
                )
            self.run(
                [
                    self.bin + "pg_ctl",
                    "-D",
                    str(data),
                    "-l",
                    str(data / "server.log"),
                    "-w",
                    "-t",
                    "60",
                    "start",
                ],
                user=True,
            )
            try:
                env = {
                    "PGHOST": str(data),
                    "PGPORT": "55439",
                    "PGUSER": self.service["user"],
                    "PGDATABASE": test["setup"]["database"],
                    "PATH": self.service["prefix"] + "/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                }
                if env["PGDATABASE"] != "postgres":
                    self.run(
                        [self.bin + "createdb", env["PGDATABASE"]],
                        user=True,
                        env={**env, "PGDATABASE": "postgres"},
                    )
                if test["kind"] == "command":
                    argv = [arg.replace("{prefix}", self.service["prefix"]) for arg in test["argv"]]
                else:
                    script = Path("/tmp") / ("acceptance-" + test["id"] + ".sql")
                    script.write_text(test["sql"])
                    script.chmod(0o644)
                    argv = [
                        self.bin + "psql",
                        "-X",
                        "-A",
                        "-t",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-v",
                        "VERBOSITY=verbose",
                        "-f",
                        str(script),
                    ]
                sqlstate_assertion = test["kind"] != "command" and any(
                    a["source"] == "sqlstate" for a in test["assertions"]
                )
                stdout = self.run(
                    argv,
                    user=True,
                    env=env,
                    timeout=test["timeout_seconds"],
                    accepted_exit_codes=(0, 3) if sqlstate_assertion else (0,),
                )
                stderr = (self.output / (str(self.sequence) + ".stderr.log")).read_text()
                sqlstate = "00000"
                if self.last_returncode:
                    match = re.search(r"ERROR:\s+([0-9A-Z]{5}):", stderr)
                    if not match:
                        raise RuntimeError("SQL failure has no valid SQLSTATE evidence")
                    sqlstate = match[1]
                outputs = {
                    "stdout": stdout,
                    "sql": stdout,
                    "stderr": stderr,
                    "server-log": (data / "server.log").read_text(),
                    "sqlstate": sqlstate,
                }
                failures = []
                for assertion in test["assertions"]:
                    value = outputs[assertion["source"]]
                    expected = assertion.get("equals")
                    if isinstance(expected, bool):
                        expected = "t" if expected else "f"
                    passed = (
                        assertion["contains"] in value
                        if "contains" in assertion
                        else value.strip() == str(expected)
                    )
                    if not passed:
                        failures.append(assertion["id"])
                expectation = test["expectations"][self.config["branch"]]
                passed = (
                    not failures
                    if expectation["outcome"] == "pass"
                    else failures == [expectation["assertion_id"]]
                )
                self.check("acceptance:" + test["id"], passed, failed_assertions=failures)
            finally:
                original_error = sys.exc_info()[1]
                try:
                    self.run(
                        [
                            self.bin + "pg_ctl",
                            "-D",
                            str(data),
                            "-w",
                            "-t",
                            "60",
                            "-m",
                            "immediate",
                            "stop",
                        ],
                        user=True,
                    )
                except Exception:
                    if original_error is None:
                        raise

    def upgrade(self):
        previous = self.config["previous"]
        shutil.copytree("/work/source/previous", "/tmp/previous")
        Path("/tmp/previous").chmod(0o755)
        for path in Path("/tmp/previous").iterdir():
            path.chmod(0o644)
        for package in previous["packages"]:
            current = next(p for p in self.config["packages"] if p["role"] == package["role"])
            self.run(
                ["dpkg", "--compare-versions", package["version"], "lt", current["deb_version"]]
            )
        self.run(
            [
                *self.apt,
                "install",
                *("/tmp/previous/" + p["filename"] for p in previous["packages"]),
            ]
        )
        self.initialize(self.data)
        config = self.data / "postgresql.conf"
        with config.open("a") as stream:
            stream.write(
                "\nlisten_addresses = ''\nunix_socket_directories = '"
                + self.service["socket"]
                + "'\n"
            )
        self.systemctl("start")
        if self.sql("SHOW server_version").split()[0] != previous["postgresql_version"]:
            raise RuntimeError("previous package server version differs from declared input")
        self.sql(
            "CREATE TABLE pg_maker_sentinel(value integer); "
            "INSERT INTO pg_maker_sentinel VALUES (42)"
        )
        original, uid, gid = config.read_bytes(), self.data.stat().st_uid, self.data.stat().st_gid
        self.run([*self.apt, "install", *self.package_paths])
        self.systemctl("status")
        self.check(
            "previous-release-upgrade",
            self.sql("SHOW server_version").split()[0] == self.config["postgresql_version"]
            and self.sql("SELECT sum(value) FROM pg_maker_sentinel") == "42"
            and config.read_bytes() == original
            and (self.data.stat().st_uid, self.data.stat().st_gid) == (uid, gid),
        )
        self.systemctl("stop")
        self.run([*self.apt, "purge", *self.package_names])

    def execute(self):
        self.prepare()
        if self.config.get("previous"):
            self.upgrade()
            return
        if "server" in self.config["roles"]:
            server = next(p for p in self.config["packages"] if p["role"] == "server")
            self.run(
                [*self.apt, "--simulate", "install", "/tmp/packages/" + Path(server["path"]).name],
                expected=1,
            )
            self.check("missing-internal-dependency-rejected")
        self.run([*self.apt, "install", *self.package_paths])
        self.installed_inventory()
        self.documentation()
        if "server" not in self.config["roles"]:
            self.check(
                "no-cluster-or-service",
                not self.data.exists()
                and not Path("/usr/lib/systemd/system", self.service["name"] + ".service").exists(),
            )
            if "client" in self.config["roles"]:
                self.check(
                    "client-version",
                    self.config["postgresql_version"] in self.run([self.bin + "psql", "--version"]),
                )
            self.run([*self.apt, "purge", *self.package_names])
            self.check("standalone-remove")
            return
        self.check("install-does-not-initdb", not (self.data / "PG_VERSION").exists())
        self.systemctl("start", expected=1)
        self.check("empty-pgdata-start-rejected")
        if not self.config["run_database"]:
            self.run([*self.apt, "purge", *self.package_names])
            self.check("remove-preserves-empty-pgdata", self.data.is_dir())
            return
        self.initialize(self.data)
        config = self.data / "postgresql.conf"
        with config.open("a") as stream:
            stream.write(
                "\nlisten_addresses = '127.0.0.1'\nport = "
                + self.service["port"]
                + "\nlogging_collector = on\nlog_directory = 'log'\n"
            )
            preload = [
                name
                for component in self.config["components"]
                for name in component.get("preload", [])
            ]
            if preload:
                stream.write(
                    "shared_preload_libraries = '" + ",".join(preload).replace("'", "''") + "'\n"
                )
        self.systemctl("start")
        self.systemctl("status")
        self.systemctl("reload")
        self.check("service-start-status-reload")
        self.default_socket()
        self.sql('ALTER USER "' + self.service["user"] + "\" PASSWORD 'pg_maker_disposable_test'")
        self.check(
            "socket-and-tcp", self.sql("SELECT 1") == "1" and self.sql("SELECT 1", tcp=True) == "1"
        )
        self.check(
            "installed-server-version",
            self.sql("SHOW server_version").split()[0] == self.config["postgresql_version"],
        )
        self.sql(
            "CREATE TABLE pg_maker_sentinel (value integer); "
            "INSERT INTO pg_maker_sentinel VALUES (42)"
        )
        self.sql("BEGIN; INSERT INTO pg_maker_sentinel VALUES (7); ROLLBACK")
        for component in self.config["components"]:
            if component.get("shared_library"):
                self.sql("LOAD '" + component["shared_library"].replace("'", "''") + "'")
            if component.get("sql_extension"):
                self.sql('CREATE EXTENSION "' + component["sql_extension"].replace('"', '""') + '"')
            for test in component.get("installed_checks", []):
                output = self.run(
                    [a.replace("{prefix}", self.service["prefix"]) for a in test["argv"]],
                    user=True,
                    timeout=test["timeout_seconds"],
                )
                self.check(
                    "component:" + component["id"], test.get("stdout_contains", "") in output
                )
        old_pid = (self.data / "postmaster.pid").read_text().splitlines()[0]
        self.systemctl("restart")
        self.check(
            "restart-preserves-data",
            self.sql("SELECT sum(value) FROM pg_maker_sentinel") == "42"
            and old_pid != (self.data / "postmaster.pid").read_text().splitlines()[0],
        )
        backup = self.data / "pg-maker-test.dump"
        env = {
            "PGHOST": self.service["socket"],
            "PGPORT": self.service["port"],
            "PGUSER": self.service["user"],
        }
        self.run([self.bin + "pg_dump", "-Fc", "-f", str(backup), "postgres"], user=True, env=env)
        self.run([self.bin + "createdb", "pg_maker_restore"], user=True, env=env)
        self.run(
            [self.bin + "pg_restore", "-d", "pg_maker_restore", str(backup)], user=True, env=env
        )
        self.check(
            "dump-restore",
            self.sql("SELECT sum(value) FROM pg_maker_sentinel", database="pg_maker_restore")
            == "42",
        )
        pid = int((self.data / "postmaster.pid").read_text().splitlines()[0])
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + 30
        while True:
            # SIGKILL can leave the socket pathname; wait for its listener to close.
            state = Path(f"/proc/{pid}/stat")
            try:
                alive = state.read_text().rsplit(")", 1)[1].split()[0] != "Z"
            except FileNotFoundError:
                alive = False
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                try:
                    listening = (
                        probe.connect_ex(
                            str(Path(self.service["socket"]) / (".s.PGSQL." + self.service["port"]))
                        )
                        == 0
                    )
                except OSError:
                    listening = False
            if not alive and not listening:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("crashed postmaster did not release resources in time")
            time.sleep(0.1)
        self.systemctl("start")
        self.check("crash-recovery", self.sql("SELECT sum(value) FROM pg_maker_sentinel") == "42")
        self.systemctl("stop")
        self.run([self.bin + "pg_checksums", "--check", "-D", str(self.data)], user=True)
        self.check("checksums")
        original = config.read_bytes()
        with config.open("a") as stream:
            stream.write("\npg_maker_invalid_setting = true\n")
        self.systemctl("start", expected=1)
        config.write_bytes(original)
        self.data.chmod(0)
        try:
            self.systemctl("start", expected=1)
        finally:
            self.data.chmod(0o700)
        with socket.socket() as blocker:
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            blocker.bind(("127.0.0.1", int(self.service["port"])))
            blocker.listen()
            self.systemctl("start", expected=1)
        lock = Path(self.service["socket"]) / (".s.PGSQL." + self.service["port"] + ".lock")
        lock.write_text(
            str(os.getpid()) + "\n" + str(self.data) + "\n" + str(int(time.time())) + "\n"
        )
        try:
            self.systemctl("start", expected=1)
        finally:
            lock.unlink(missing_ok=True)
        self.check("invalid-config-permissions-port-socket-rejected")
        self.systemctl("start")
        self.run([*self.apt, "--reinstall", "install", *self.package_paths])
        self.check(
            "reinstall-preserves-config-data",
            config.read_bytes() == original
            and self.sql("SELECT sum(value) FROM pg_maker_sentinel") == "42",
        )
        if not self.config.get("previous_declared"):
            self.results.append(
                {
                    "id": "previous-release-upgrade",
                    "status": "not-applicable",
                    "reason": "first release; no previous package set declared",
                }
            )
        self.acceptance()
        self.check(
            "config-and-file-logs-in-pgdata",
            (self.data / "server.log").is_file() and (self.data / "log").is_dir(),
        )
        self.run([*self.apt, "remove", *self.package_names])
        self.check(
            "remove-preserves-data",
            config.read_bytes() == original and (self.data / "PG_VERSION").is_file(),
        )
        self.run([*self.apt, "install", *self.package_paths])
        self.systemctl("start")
        self.check(
            "install-after-remove", self.sql("SELECT sum(value) FROM pg_maker_sentinel") == "42"
        )
        self.run([*self.apt, "purge", *self.package_names])
        self.check(
            "purge-preserves-data",
            config.read_bytes() == original and (self.data / "PG_VERSION").is_file(),
        )


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    harness = Harness(config, sys.argv[2])
    try:
        harness.execute()
        harness.save(True)
        return 0
    except Exception as exc:
        harness.save(False, type(exc).__name__ + ": " + str(exc))
        raise


if __name__ == "__main__":
    raise SystemExit(main())
