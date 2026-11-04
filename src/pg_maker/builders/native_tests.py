"""Explicit upstream test commands for non-PGXS component build systems."""

import re
from contextlib import nullcontext
from pathlib import Path

from pg_maker.errors import CommandError, PreconditionError
from pg_maker.storage import write_json


def substitutions(branch, component, cwd):
    values = {
        "{prefix}": branch.prefix,
        "{component}": cwd,
        "{build}": cwd + "/pg-maker-build",
        "{postgresql_source}": branch.container_root + "/source",
        "{postgresql_build}": branch.container_root + "/obj",
        "{jobs}": str(branch.variant["build"]["jobs"]),
        "{test_helpers}": "/work/source/upstream-tests",
    }
    for name in component["spec"].get("extra_sources", {}):
        values["{source:" + name + "}"] = (
            branch.container_root + "/extra-sources/" + component["metadata"]["id"] + "/" + name
        )
    return values


def render(text, values):
    for token, value in values.items():
        text = text.replace(token, value)
    return text


def run(branch, component, cwd):
    from pg_maker.builders.components import cluster

    spec, identifier = component["spec"], component["metadata"]["id"]
    values = substitutions(branch, component, cwd)
    commands = spec.get("test_commands", [])
    if not commands:
        raise PreconditionError("component has no explicit native tests")
    base_env = {
        "PATH": branch.prefix + "/bin:/usr/local/bin:/usr/bin:/bin",
        "PERL5LIB": branch.container_root + "/source/src/test/perl",
        "PG_REGRESS": branch.container_root + "/obj/src/test/regress/pg_regress",
        "TZ": "UTC",
        "PGTZ": "UTC",
    }
    use_cluster = spec.get("test_cluster", True)
    manager = (
        cluster(
            branch,
            "native-" + identifier,
            database=spec.get("test_database", "postgres"),
            preload=spec.get("preload", []),
            settings=spec.get("test_settings", {}),
        )
        if use_cluster
        else nullcontext(({}, None))
    )
    outcomes = []
    with manager as (cluster_env, _):
        env = {**base_env, **cluster_env}
        values.update(
            {"{port}": env.get("PGPORT", "55432"), "{user}": env.get("PGUSER", "builder")}
        )
        for setup in spec.get("test_setup", []):
            branch.command(
                [render(arg, values) for arg in setup], cwd=cwd, env=env, test_command=True
            )
        for test in commands:
            workdir = render(test.get("cwd", "{component}"), values)
            test_env = {**env, **{k: render(v, values) for k, v in test.get("env", {}).items()}}
            result = branch.command(
                [render(arg, values) for arg in test["argv"]],
                cwd=workdir,
                env=test_env,
                test_command=True,
            )
            text = Path(result.stdout_log).read_text(errors="replace") + Path(
                result.stderr_log
            ).read_text(errors="replace")
            matched = list(re.finditer(test["success_pattern"], text, re.M))
            counts = [int(m.group("count")) if "count" in m.groupdict() else 1 for m in matched]
            passed = bool(counts) and all(n > 0 for n in counts)
            outcomes.append(
                {
                    "id": test["id"],
                    "passed": passed,
                    "reported_counts": counts,
                    "count_kind": "tests"
                    if matched and "count" in matched[0].groupdict()
                    else "successful-suite-markers",
                }
            )
            write_json(branch.output / "components" / identifier / "native-tests.json", outcomes)
            if not passed:
                raise CommandError(
                    "native tests did not report a nonempty successful result",
                    details={"component": identifier, "test": test["id"]},
                )
    return {
        "id": identifier,
        "built": True,
        "native_targets": [t["id"] for t in commands],
        "tests": "passed",
        "passed": True,
        "native_tests": outcomes,
    }
