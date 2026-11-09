"""Explicit Make/PGXS components and fresh-cluster acceptance checks."""

import hashlib
import re
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

from pg_maker.builders.autotools import regression_evidence, schedule_tests
from pg_maker.documents import relative_output
from pg_maker.errors import CommandError, MakerError, PreconditionError, ValidationError
from pg_maker.storage import file_hash, read_json, write_json
from pg_maker.yamlio import load_yaml


def integrate(branch, components):
    for component in components:
        spec, identifier = component["spec"], component["metadata"]["id"]
        if spec["type"] != "source-tree":
            continue
        relative = spec.get("source_directory")
        if not relative or not relative_output(relative):
            raise ValidationError("source-tree component requires a relative integration directory")
        destination = branch.source / relative
        if destination.exists() or destination.is_symlink():
            raise PreconditionError("source-tree component must not overwrite PostgreSQL files")
        if any(p.is_symlink() for p in destination.parents):
            raise ValidationError("component destination traverses a symlink")
        shutil.copytree(
            branch.inputs.tree(spec["source"]["source_hash"]), destination, symlinks=True
        )
        branch.report.setdefault("integrated_components", []).append(identifier)


@contextmanager
def cluster(branch, name, *, database="postgres", preload=(), settings=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValidationError("invalid cluster identifier")
    root = branch.root / "clusters" / name
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    container = branch.container_root + "/clusters/" + name
    data = container + "/data"
    sockets = "/tmp/pg-maker-socket-" + hashlib.sha256(name.encode()).hexdigest()[:16]
    branch.command(["mkdir", "-p", sockets])
    prefix = branch.prefix
    env = {
        "PGHOST": sockets,
        "PGPORT": "55432",
        "PGUSER": "builder",
        "PGDATABASE": database,
        "PATH": prefix + "/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C.UTF-8",
    }
    branch.command(
        [
            prefix + "/bin/initdb",
            "-D",
            data,
            "-A",
            "trust",
            "--no-locale",
            "--encoding=UTF8",
            "-U",
            "builder",
        ]
    )
    configuration = (
        "listen_addresses = ''\nport = 55432\nunix_socket_directories = '" + sockets + "'\n"
    )
    if preload:
        if any(not re.fullmatch(r"[A-Za-z0-9_/-]+", item) for item in preload):
            raise ValidationError("invalid preload library")
        configuration += "shared_preload_libraries = '" + ",".join(preload) + "'\n"
    for key, value in (settings or {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]*", key):
            raise ValidationError("invalid test configuration setting")
        configuration += key + " = '" + str(value).replace("'", "''").replace("\\", "\\\\") + "'\n"
    with (root / "data/postgresql.conf").open("a") as stream:
        stream.write(configuration)
    # pg_ctl readiness is bounded; all PostgreSQL file logs stay under PGDATA.
    branch.command(
        [prefix + "/bin/pg_ctl", "-D", data, "-l", data + "/server.log", "-w", "-t", "60", "start"]
    )
    try:
        if database != "postgres":
            branch.command(
                [prefix + "/bin/createdb", database], env={**env, "PGDATABASE": "postgres"}
            )
        yield env, root / "data/server.log"
    finally:
        # Cancellation stops the entire owned container; no unrelated host process is touched.
        if not branch.context.token.requested():
            original_error = sys.exc_info()[0] is not None
            try:
                branch.command(
                    [
                        prefix + "/bin/pg_ctl",
                        "-D",
                        data,
                        "-w",
                        "-t",
                        "60",
                        "-m",
                        "immediate",
                        "stop",
                    ]
                )
            except MakerError:
                # A timed-out command may already have stopped the container. Preserve its
                # failure; the outer engine still releases/verifies the entire owned resource.
                if not original_error:
                    raise


def verify_extension_versions(branch, spec):
    """Check delivered control files even in quick mode; this is not database qualification."""
    for name, expected in spec.get("sql_extension_versions", {}).items():
        control = branch.root / "prefix/share/postgresql/extension" / (name + ".control")
        if not control.is_file():
            raise PreconditionError(
                "extension control file is missing", details={"extension": name}
            )
        found = re.search(r"^\s*default_version\s*=\s*'([^']+)'", control.read_text(), re.M)
        if not found or found[1] != expected:
            raise PreconditionError(
                "extension version differs from recipe",
                details={
                    "extension": name,
                    "expected": expected,
                    "actual": found[1] if found else None,
                },
            )


def build_components(branch, components, *, run_tests=True):
    from pg_maker.builders import native_tests
    from pg_maker.execution.install import ComponentInstall
    from pg_maker.execution.workspace import archive_evidence

    reports = []
    for component in components:
        spec, identifier = component["spec"], component["metadata"]["id"]
        kind = spec["type"]
        branch.report["components"] = [
            r for r in branch.report.get("components", []) if r["id"] != identifier
        ]
        evidence = branch.output / "components" / identifier
        archive_evidence(branch, evidence)
        installation = ComponentInstall(branch, identifier).begin()
        if kind in {"contrib", "source-tree"}:
            relative = spec.get("source_directory", "contrib/" + identifier)
            if not (branch.source / relative / "Makefile").is_file():
                raise PreconditionError("component source directory is missing")
            cwd = branch.container_root + "/obj/" + relative
            (branch.obj / relative).mkdir(parents=True, exist_ok=True)
            component_source = branch.source / relative
            container_source = branch.container_root + "/source/" + relative
        else:
            path = branch.root / "components" / identifier
            if not path.exists():
                shutil.copytree(
                    branch.inputs.tree(spec["source"]["source_hash"]), path, symlinks=True
                )
            cwd = branch.container_root + "/components/" + identifier
            if spec.get("source_directory"):
                cwd += "/" + spec["source_directory"]
                path /= spec["source_directory"]
            component_source, container_source = path, cwd
            configure_args = [
                native_tests.render(arg, native_tests.substitutions(branch, component, cwd))
                for arg in spec.get("configure_args", [])
            ]
            for name, source in spec.get("extra_sources", {}).items():
                extra_path = branch.root / "extra-sources" / identifier / name
                if not extra_path.exists():
                    shutil.copytree(
                        branch.inputs.tree(source["source_hash"]), extra_path, symlinks=True
                    )
                extra_container = (
                    branch.container_root + "/extra-sources/" + identifier + "/" + name
                )
                configure_args = [
                    arg.replace("{source:" + name + "}", extra_container) for arg in configure_args
                ]
            if spec.get("build_commands"):
                values = native_tests.substitutions(branch, component, cwd)
                for command in spec["build_commands"]:
                    branch.command(
                        [native_tests.render(arg, values) for arg in command["argv"]],
                        cwd=cwd,
                        env={
                            "DESTDIR": installation.container,
                            **{
                                k: native_tests.render(v, values)
                                for k, v in command.get("env", {}).items()
                            },
                        },
                    )
                installation.commit()
                verify_extension_versions(branch, spec)
                reports.append(
                    native_tests.run(branch, component, cwd)
                    if run_tests
                    else {"id": identifier, "built": True, "native_targets": [], "tests": "skipped"}
                )
                continue
            if spec.get("build_system") == "cmake":
                build_path = cwd + "/pg-maker-build"
                branch.command(
                    [
                        "cmake",
                        "-S",
                        cwd,
                        "-B",
                        build_path,
                        "-DCMAKE_INSTALL_PREFIX=" + branch.prefix,
                        "-DCMAKE_INSTALL_RPATH=" + branch.prefix + "/lib",
                        *configure_args,
                    ],
                    cwd=cwd,
                )
                branch.command(
                    [
                        "cmake",
                        "--build",
                        build_path,
                        "--parallel",
                        str(branch.variant["build"]["jobs"]),
                    ],
                    cwd=cwd,
                )
                branch.command(
                    ["cmake", "--install", build_path],
                    cwd=cwd,
                    env={"DESTDIR": installation.container},
                )
                installation.commit()
                verify_extension_versions(branch, spec)
                reports.append(
                    native_tests.run(branch, component, cwd)
                    if run_tests
                    else {"id": identifier, "built": True, "native_targets": [], "tests": "skipped"}
                )
                continue
            if spec.get("configure_args") or (
                kind in {"utility", "library"} and (path / "configure").is_file()
            ):
                branch.command(
                    [
                        "./configure",
                        "--prefix=" + branch.prefix,
                        *configure_args,
                    ],
                    cwd=cwd,
                )
        make = ["make", "--output-sync=target", "-j", str(branch.variant["build"]["jobs"])]
        if kind in {"contrib", "source-tree"}:
            make += ["-f", container_source + "/Makefile"]
        if kind == "pgxs":
            make += ["USE_PGXS=1", "PG_CONFIG=" + branch.prefix + "/bin/pg_config"]
        elif kind in {"utility", "library"}:
            make += ["prefix=" + branch.prefix, "PG_CONFIG=" + branch.prefix + "/bin/pg_config"]
        if spec["make_targets"]:
            branch.command([*make, *spec["make_targets"]], cwd=cwd)
        if spec["install_targets"]:
            branch.command(
                [*make, "DESTDIR=" + installation.container, *spec["install_targets"]], cwd=cwd
            )
        installation.commit()
        verify_extension_versions(branch, spec)
        if not run_tests:
            reports.append(
                {"id": identifier, "built": True, "native_targets": [], "tests": "skipped"}
            )
            continue
        if spec.get("test_commands"):
            reports.append(native_tests.run(branch, component, cwd))
            continue
        expected = None
        allowed_skips = {}
        test_evidence = []
        substitutions = native_tests.substitutions(branch, component, cwd)
        test_make = [
            *make,
            *[native_tests.render(a, substitutions) for a in spec.get("test_make_args", [])],
        ]
        if kind in {"pgxs", "contrib", "source-tree"} and spec["test_targets"]:
            result = branch.command(
                [
                    *test_make,
                    *(["-f", "Makefile"] if kind == "pgxs" else []),
                    "-f",
                    "/work/source/inventory.mk",
                    "maker-inventory",
                ],
                cwd=cwd,
            )
            values = dict(
                re.findall(r"^PG_MAKER_([A-Z_]+)=(.*)$", Path(result.stdout_log).read_text(), re.M)
            )
            expected = {
                "regression": values["REGRESS"].split(),
                "isolation": values["ISOLATION"].split(),
                "tap": [
                    p.relative_to(component_source).as_posix()
                    for p in sorted((component_source / "t").glob("*.pl"))
                ]
                if values["TAP"] == "1"
                else [],
            }
            for schedule in re.findall(r"--schedule=(\S+)", values.get("REGRESS_OPTS", "")):
                path = Path(schedule)
                if str(path).startswith(container_source + "/"):
                    path = component_source / str(path).removeprefix(container_source + "/")
                elif not path.is_absolute():
                    path = component_source / path
                expected["regression"].extend(schedule_tests(path))
            if not any(expected.values()):
                raise PreconditionError("component has no declared native test inventory")
            allowed_skips = component_tap_skip_policy(spec, component_source, expected)
            write_json(
                branch.output / "components" / identifier / "expected.json",
                {**expected, "allowed_tap_skips": list(allowed_skips.values())},
            )
        with cluster(
            branch,
            "component-" + identifier,
            preload=spec.get("preload", []),
            settings=spec.get("test_settings", {}),
        ) as (
            env,
            _,
        ):
            if spec.get("shared_library"):
                library = spec["shared_library"].replace("'", "''")
                branch.command(
                    [
                        branch.prefix + "/bin/psql",
                        "-X",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-c",
                        "LOAD '" + library + "'",
                    ],
                    env=env,
                )
            if spec.get("sql_extension"):
                extension = spec["sql_extension"].replace('"', '""')
                branch.command(
                    [
                        branch.prefix + "/bin/psql",
                        "-X",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-c",
                        'CREATE EXTENSION "' + extension + '" CASCADE',
                    ],
                    env=env,
                )
            for target in spec["test_targets"]:
                tap = branch.output / "components" / identifier / target / "tap"
                tap.mkdir(parents=True)
                test_env = {
                    **env,
                    "PERL5LIB": "/work/source:" + branch.container_root + "/source/src/test/perl",
                    "PG_MAKER_SOURCE": container_source,
                    "PG_MAKER_TAP": "/work/output/"
                    + branch.id
                    + "/components/"
                    + identifier
                    + "/"
                    + target
                    + "/tap",
                }
                result = branch.command(
                    [*test_make, target, "PROVE_FLAGS=--formatter=PgMaker::Formatter"],
                    cwd=cwd,
                    env=test_env,
                    test_command=True,
                )
                if expected is not None:
                    evidence = verify_component_tests(
                        expected,
                        Path(result.stdout_log).read_text(),
                        tap,
                        allowed_skips=allowed_skips,
                    )
                    test_evidence.append({"target": target, **evidence})
                    write_json(
                        branch.output / "components" / identifier / "test-evidence.json",
                        test_evidence,
                    )
        reports.append(
            {
                "id": identifier,
                "built": True,
                "native_targets": spec["test_targets"],
                "native_test_evidence": test_evidence,
                "test_exceptions": [
                    item for result in test_evidence for item in result["excluded_tap_suites"]
                ],
                "tests": "passed",
                "passed": True,
            }
        )
    branch.report.setdefault("components", []).extend(reports)


def component_tap_skip_policy(spec, source, expected):
    """Bind an operator exception to the exact inventoried source file."""
    allowed = {}
    for item in spec.get("allowed_tap_skips", []):
        name = item["path"]
        path = source / name
        if (
            not relative_output(name)
            or name not in expected["tap"]
            or name in allowed
            or any(p.is_symlink() for p in (path, *path.parents))
            or not path.is_file()
            or file_hash(path) != item["source_sha256"]
        ):
            raise PreconditionError(
                "TAP skip exception source or inventory mismatch", details={"path": name}
            )
        allowed[name] = dict(item)
    return allowed


def verify_component_tests(expected, stdout, tap, *, allowed_skips=None):
    evidence = regression_evidence(stdout)
    for kind in ("regression", "isolation"):
        values = [v for (k, _), v in evidence.items() if k == kind]
        actual = [name for v in values for name in v["tests"]]
        if sorted(expected[kind]) != sorted(actual) or any(
            v["failed"] or v["plans"] != [len(v["tests"])] for v in values
        ):
            raise CommandError("component regression inventory mismatch", details={"kind": kind})
    values = [read_json(p) for p in tap.glob("*.json")]
    if sorted(v["path"] for v in values) != sorted(expected["tap"]):
        raise CommandError("component TAP inventory mismatch")
    allowed_skips = allowed_skips or {}
    excluded = []
    assertions = 0
    for value in values:
        if value["failed"] or value["errors"] or value["exit"] or value["wait"]:
            raise CommandError("component TAP execution failed", details={"path": value["path"]})
        if value["skip_all"]:
            allowance = allowed_skips.get(value["path"])
            if (
                allowance is None
                or value["skip_all"] != allowance["skip_reason"]
                or value["tests"] != 0
                or value["planned"] != 0
                or value["skipped"]
            ):
                raise CommandError("component TAP skip is not explicitly allowed")
            excluded.append({**allowance, "status": "excluded", "counted_as_passed": False})
        else:
            if value["tests"] <= len(value["skipped"]) or value["tests"] != value["planned"]:
                raise CommandError("component TAP inventory mismatch")
            assertions += value["tests"] - len(value["skipped"])
    counts = {
        "regression_tests": len(expected["regression"]),
        "isolation_tests": len(expected["isolation"]),
        "tap_assertions": assertions,
    }
    if sum(counts.values()) == 0:
        raise CommandError("component has no executed passing tests")
    return {**counts, "excluded_tap_suites": excluded}


def acceptance(branch, changes):
    reports, seen = [], set()
    for change in changes:
        for test in change["spec"]["acceptance"]:
            if branch.branch not in test["expectations"]:
                continue
            if test["id"] in seen:
                continue
            seen.add(test["id"])
            blob = branch.inputs.blob(test["script"]["sha256"])
            script = branch.helper / ("acceptance-" + test["id"])
            shutil.copyfile(blob, script)
            script_path = "/work/source/" + script.name
            with cluster(
                branch, "acceptance-" + test["id"], database=test["setup"]["database"]
            ) as (env, log):
                if test["kind"] == "command":
                    document = load_yaml(blob)
                    if (
                        not isinstance(document, dict)
                        or set(document) != {"argv"}
                        or not isinstance(document["argv"], list)
                        or not document["argv"]
                        or not all(isinstance(v, str) and "\x00" not in v for v in document["argv"])
                    ):
                        raise ValidationError(
                            "command acceptance script must be YAML {argv: [program, arguments...]}"
                        )
                    argv = [arg.replace("{prefix}", branch.prefix) for arg in document["argv"]]
                else:
                    argv = [
                        branch.prefix + "/bin/psql",
                        "-X",
                        "-A",
                        "-t",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-v",
                        "VERBOSITY=verbose",
                        "-f",
                        script_path,
                    ]
                sqlstate_assertion = test["kind"] != "command" and any(
                    a["source"] == "sqlstate" for a in test["assertions"]
                )
                result = branch.command(
                    argv,
                    env=env,
                    timeout=test["timeout_seconds"],
                    accepted_exit_codes=(0, 3) if sqlstate_assertion else (0,),
                    test_command=True,
                )
                stdout, stderr = (
                    Path(result.stdout_log).read_text(),
                    Path(result.stderr_log).read_text(),
                )
                outputs = {
                    "stdout": stdout,
                    "stderr": stderr,
                    "sql": stdout,
                    "server-log": log.read_text(),
                    "sqlstate": sqlstate(result.returncode, stderr),
                }
                failures = []
                for assertion in test["assertions"]:
                    output = outputs[assertion["source"]]
                    if "contains" in assertion:
                        passed = assertion["contains"] in output
                    else:
                        expected = assertion["equals"]
                        if isinstance(expected, bool):
                            expected = "t" if expected else "f"
                        passed = output.strip() == str(expected)
                    if not passed:
                        failures.append(assertion["id"])
                expectation = test["expectations"][branch.branch]
                passed = (
                    not failures
                    if expectation["outcome"] == "pass"
                    else failures == [expectation.get("assertion_id")]
                )
                report = {
                    "id": test["id"],
                    "expectation": expectation,
                    "failed_assertions": failures,
                    "passed": passed,
                }
                reports.append(report)
                write_json(branch.output / "acceptance.json", reports)
                if not passed:
                    raise CommandError("acceptance expectation mismatch", details=report)
    branch.report["acceptance"] = reports


def sqlstate(returncode, stderr):
    if returncode == 0:
        return "00000"
    match = re.search(r"ERROR:\s+([0-9A-Z]{5}):", stderr)
    if returncode != 3 or not match:
        raise CommandError("SQL failure has no valid SQLSTATE evidence")
    return match[1]
