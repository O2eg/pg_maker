"""Autotools execution policy and the PostgreSQL 18 DB-test evidence adapter."""

from __future__ import annotations

import re
from pathlib import Path

from pg_maker.errors import CommandError, PreconditionError
from pg_maker.storage import read_json, write_json

# Evaluated by make inside the build container, after configure. No controller-side make.
INVENTORY_MAKE = """
.PHONY: maker-inventory
maker-inventory:
\t@$(info PG_MAKER_SUBDIRS=$(SUBDIRS))true
\t@$(info PG_MAKER_REGRESS=$(REGRESS))true
\t@$(info PG_MAKER_ISOLATION=$(ISOLATION))true
\t@$(info PG_MAKER_REGRESS_OPTS=$(REGRESS_OPTS))true
\t@$(info PG_MAKER_TAP=$(TAP_TESTS))true
"""

# TAP::Parser supplies machine evidence, independent of make/prove exit status and console format.
TAP_FORMATTER = r"""
package PgMaker::Formatter;
use strict;
use warnings;
use parent 'TAP::Formatter::Console';
sub open_test {
    my ($self, $name, $parser) = @_;
    my $session = $self->SUPER::open_test($name, $parser);
    bless $session, 'PgMaker::Session';
    return $session;
}
package PgMaker::Session;
use parent 'TAP::Formatter::Console::Session';
use JSON::PP;
use Cwd qw(abs_path);
use Digest::SHA qw(sha256_hex);
sub close_test {
    my ($self) = @_;
    my $p = $self->parser;
    my $name = abs_path($self->name);
    my $root = $ENV{PG_MAKER_SOURCE};
    die 'test outside source' unless index($name, "$root/") == 0;
    my $rel = substr($name, length($root) + 1);
    my $out = $ENV{PG_MAKER_TAP} . '/' . sha256_hex($rel) . '.json';
    open(my $fh, '>', "$out.$$") or die $!;
    print $fh JSON::PP->new->canonical->encode({
        path => $rel, tests => 0 + $p->tests_run, planned => 0 + ($p->tests_planned // 0),
        failed => [$p->failed], skipped => [$p->skipped], errors => [$p->parse_errors],
        exit => 0 + $p->exit, wait => 0 + $p->wait, skip_all => ($p->skip_all // ''),
    });
    close($fh) or die $!;
    rename("$out.$$", $out) or die $!;
    $self->SUPER::close_test;
}
1;
"""


def extra_tests(build: dict, recipe: dict) -> str:
    # GSSAPI/LDAP/OAuth need additional service fixtures; refuse their configure options below.
    return " ".join([*(["ssl"] if build["options"]["openssl"] else []), *recipe["extra_tests"]])


def verify_configure(argv, settings: str, header: str, output: str) -> dict:
    """Check the effective Autoconf result before any compilation."""
    if re.search(r"unrecognized (?:options?|arguments?)", output, re.I):
        raise PreconditionError("configure reported unrecognized options")
    values = dict(re.findall(r"^([A-Za-z_]+)\s*:?=\s*(.*?)\s*$", settings, re.M))
    macros = set(re.findall(r"^#define ([A-Z_0-9]+)\s+1$", header, re.M))
    expected = {"enable_tap_tests": "yes"}
    macro_options = {
        "lz4": "USE_LZ4",
        "zstd": "USE_ZSTD",
        "pam": "USE_PAM",
        "cassert": "USE_ASSERT_CHECKING",
    }
    for arg in argv[1:]:
        if arg.startswith("--prefix="):
            expected["prefix"] = arg.split("=", 1)[1]
        match = re.fullmatch(r"--(with|without|enable|disable)-([a-z0-9-]+)(?:=(.*))?", arg)
        if not match:
            continue
        action, name, value = match.groups()
        enabled = action in {"with", "enable"} and value != "no"
        if name in macro_options:
            if (macro_options[name] in macros) != enabled:
                raise PreconditionError(
                    "configured option differs from plan", details={"option": name}
                )
        else:
            key = ("with_" if action in {"with", "without"} else "enable_") + name.replace("-", "_")
            if key in values or name in {
                "icu",
                "perl",
                "python",
                "tcl",
                "ssl",
                "readline",
                "systemd",
                "gssapi",
                "ldap",
                "libxml",
                "libxslt",
                "llvm",
                "uuid",
                "nls",
                "tap-tests",
                "debug",
            }:
                expected[key] = (value if value and name != "nls" else "yes") if enabled else "no"
    mismatch = {
        key: {"expected": value, "actual": values.get(key)}
        for key, value in expected.items()
        if values.get(key) != value
    }
    if mismatch:
        raise PreconditionError("configured options differ from plan", details=mismatch)
    return {"passed": True, "settings": expected}


def validate_build(config: dict, goal: str, *, run_db_tests: bool = True) -> None:
    from pg_maker.errors import UnsupportedError

    if goal not in {
        "source",
        "build",
        "package",
        "verified",
        "docs",
        "translations",
        "doc-source",
        "translated-docs",
    }:
        raise UnsupportedError("unsupported build goal")
    if (
        run_db_tests
        and goal != "source"
        and config["postgresql_recipe"]["db_test_adapter"] != "pg18"
    ):
        raise UnsupportedError(
            "DB-test evidence adapter is not implemented for this PostgreSQL major; "
            "build/package with DB tests disabled remain available"
        )
    if (
        run_db_tests
        and goal != "source"
        and config["defaults"]["validation"]["profile"] == "release"
    ):
        raise UnsupportedError("extended release test fixtures are not implemented")
    for variant in config["variants"]:
        if variant["build"]["backend"] != "autotools":
            raise UnsupportedError("only Autotools execution is implemented")
        for arg in variant["build"]["configure_args"]:
            if run_db_tests and arg.startswith(
                ("--with-gssapi", "--with-ldap", "--with-oauth", "--with-curl")
            ):
                raise UnsupportedError(
                    "this configure option requires additional mandatory test fixtures"
                )
    for feature in config["features"]:
        for component in feature["spec"]["components"]:
            spec = component["spec"]
            if (
                run_db_tests
                and spec["type"] in {"pgxs", "utility", "library"}
                and not spec["test_targets"]
            ):
                raise PreconditionError("external component must declare native test targets")
            if (
                goal == "verified"
                and spec["type"] in {"utility", "library"}
                and not spec.get("installed_checks")
            ):
                raise PreconditionError("installed utility/library checks must be declared")


def schedule_tests(path: Path) -> list[str]:
    if not path.is_file():
        raise PreconditionError(
            "expected regression schedule is missing", details={"path": str(path)}
        )
    names = []
    for line in path.read_text().splitlines():
        if line.startswith("test:"):
            names.extend(line.split(":", 1)[1].split())
    if not names or len(names) != len(set(names)):
        raise PreconditionError("empty or duplicate regression schedule")
    return names


def skip_policy(path: str, source: str, config: dict) -> str | None:
    if path.endswith("/005_sspi.pl"):
        return "SSPI tests require Windows (without PG_TEST_USE_UNIX_SOCKETS)"
    if (
        "Injection points not supported by this build" in source
        and config.get("enable_injection_points") != "yes"
    ):
        return "Injection points not supported by this build"
    if path.startswith("src/test/modules/xid_wraparound/t/"):
        return "test xid_wraparound not enabled in PG_TEST_EXTRA"
    if path == "src/interfaces/libpq/t/004_load_balance_dns.pl":
        return "Potentially unsafe test load_balance not enabled in PG_TEST_EXTRA"
    if (
        path.startswith("src/test/modules/oauth_validator/t/")
        and config.get("with_oauth") != "curl"
    ):
        return "Potentially unsafe test oauth not enabled in PG_TEST_EXTRA"
    return None


def expected_inventory(source: Path, obj: Path, query) -> dict:
    """Follow configured SUBDIRS, rather than a guessed list of directories in the checkout."""
    config = dict(
        re.findall(
            r"^([A-Za-z_]+)\s*=\s*(.*?)\s*$", (obj / "src/Makefile.global").read_text(), re.M
        )
    )
    if config.get("enable_tap_tests") != "yes":
        raise PreconditionError("configure did not enable mandatory TAP tests")
    suites, visited = [], set()
    queue = [
        "src/test",
        "src/pl",
        "src/interfaces",
        "src/interfaces/ecpg/test",
        "contrib",
        "src/bin",
        "src/tools/pg_bsd_indent",
    ]
    while queue:
        directory = queue.pop(0)
        if directory in visited:
            continue
        visited.add(directory)
        makefile = source / directory / "Makefile"
        if not makefile.is_file():
            continue
        values = query(directory)
        for child in values["SUBDIRS"].split():
            if child in {".", ".."} or "/" in child:
                raise PreconditionError("unsupported configured SUBDIRS entry")
            queue.append(directory + "/" + child)
        regress = values["REGRESS"].split()
        for schedule in re.findall(r"--schedule=(\S+)", values["REGRESS_OPTS"]):
            path = Path(schedule)
            # make expands $(srcdir) to the container source path.
            if str(path).startswith("/work/build/"):
                path = source / str(path).split("/source/", 1)[1]
            elif not path.is_absolute():
                path = source / directory / path
            regress.extend(schedule_tests(path))
        if directory == "src/test/regress":
            regress = schedule_tests(source / directory / "parallel_schedule")
        if directory == "src/test/isolation":
            regress = schedule_tests(source / directory / "isolation_schedule")
        if directory == "src/interfaces/ecpg/test":
            regress = schedule_tests(source / directory / "ecpg_schedule") + ["sql/twophase"]
        if regress:
            suites.append(
                {
                    "kind": "isolation" if directory == "src/test/isolation" else "regression",
                    "directory": directory,
                    "tests": regress,
                }
            )
        isolation = values["ISOLATION"].split()
        if isolation:
            suites.append({"kind": "isolation", "directory": directory, "tests": isolation})
        if values["TAP"] == "1" or "$(prove_check)" in makefile.read_text():
            scripts = sorted((source / directory / "t").glob("*.pl"))
            if not scripts:
                raise PreconditionError("configured TAP suite has no test scripts")
            for script in scripts:
                relative = script.relative_to(source).as_posix()
                suites.append(
                    {
                        "kind": "tap",
                        "path": relative,
                        "allowed_skip": skip_policy(relative, script.read_text(), config),
                    }
                )
    if not any(s.get("directory") == "src/test/regress" for s in suites):
        raise PreconditionError("core regression inventory is empty")
    mandatory = {
        "src/test/recovery",
        "src/test/subscription",
        "src/test/authentication",
        "src/test/isolation",
        "src/interfaces/libpq",
        "contrib",
        "src/bin",
    }
    if config.get("with_ssl") == "openssl":
        mandatory.add("src/test/ssl")
    if not mandatory <= visited:
        raise PreconditionError("mandatory source test directories are missing")
    if not any(s["kind"] == "tap" for s in suites):
        raise PreconditionError("mandatory TAP inventory is empty")
    return {
        "api_version": "pg_maker/test-inventory/v1",
        "suites": suites,
        "extra_tests": config.get("PG_TEST_EXTRA", ""),
        "not_applicable": {
            key: "not configured"
            for key in ("gssapi", "ldap", "oauth")
            if config.get("with_" + key) not in {"yes", "curl"}
        },
        "extended_tests": (
            "load_balance, wal_consistency_checking, xid_wraparound require release fixtures"
        ),
    }


def regression_evidence(stdout: str) -> dict:
    """GNU make --output-sync=target keeps each test command's TAP block together.

    pg_regress deletes regression.out on success. Preserve its streamed evidence,
    using make's directory boundaries and PostgreSQL's suite markers.
    """
    current, stack, observed = ("regression", ""), [], {}
    for line in stdout.splitlines():
        entering = re.search(r"Entering directory '.*?/obj(?:/(.*?))?'$", line)
        leaving = re.search(r"Leaving directory '.*?/obj(?:/(.*?))?'$", line)
        marker = re.match(
            r"# \+\+\+ (regress|isolation) (?:check|install-check) in (\S*) \+\+\+", line
        )
        if entering:
            directory = entering[1] or ""
            stack.append(directory)
            current = ("regression", directory)
        elif leaving:
            directory = leaving[1] or ""
            if directory in stack:
                stack = stack[: len(stack) - 1 - stack[::-1].index(directory)]
            current = ("regression", stack[-1]) if stack else None
        elif marker:
            current = ("regression" if marker[1] == "regress" else "isolation", marker[2])
        elif current:
            match = re.match(r"(not )?ok\s+\d+\s+[-+]\s+(\S+)\s+\d+\s+ms$", line)
            if match:
                value = observed.setdefault(current, {"tests": [], "failed": [], "plans": []})
                value["tests"].append(match[2])
                if match[1]:
                    value["failed"].append(match[2])
            elif re.fullmatch(r"1\.\.\d+", line) and current in observed:
                observed[current]["plans"].append(int(line[3:]))
    return observed


def reconcile(
    inventory: dict, obj: Path, tap: Path, *, core_only=False, stdout: str | None = None
) -> dict:
    results, failures = [], []
    observed = {r["path"]: r for p in sorted(tap.glob("*.json")) for r in [read_json(p)]}
    regressions = regression_evidence(stdout) if stdout is not None else None
    for suite in inventory["suites"]:
        if core_only and suite.get("directory") != "src/test/regress":
            continue
        if suite["kind"] == "tap":
            value = observed.get(suite["path"])
            valid = bool(
                value
                and not value["failed"]
                and not value["errors"]
                and not value["exit"]
                and not value["wait"]
            )
            if valid and value["skip_all"]:
                valid = bool(suite["allowed_skip"] and suite["allowed_skip"] == value["skip_all"])
            elif valid:
                valid = (
                    value["tests"] > len(value["skipped"]) and value["planned"] == value["tests"]
                )
            results.append({"suite": suite, "observed": value, "passed": valid})
        else:
            directory = obj / suite["directory"]
            if suite["kind"] == "isolation":
                directory /= "output_iso"
            output = directory / "regression.out"
            text = output.read_text() if output.is_file() else ""
            names = re.findall(r"^ok\s+\d+\s+[-+]\s+(\S+)", text, re.M)
            valid = sorted(names) == sorted(suite["tests"]) and not re.search(
                r"^not ok\b", text, re.M
            )
            if regressions is not None:
                value = regressions.get((suite["kind"], suite["directory"]), {})
                names = value.get("tests", [])
                valid = (
                    sorted(names) == sorted(suite["tests"])
                    and value.get("plans") == [len(names)]
                    and not value.get("failed")
                )
            diffs = directory / "regression.diffs"
            valid = valid and (not diffs.exists() or diffs.stat().st_size == 0)
            results.append({"suite": suite, "observed": names, "passed": valid})
        if not valid:
            failures.append(suite.get("path", suite.get("directory")))
    report = {"passed": not failures, "results": results, "failures": failures}
    write_json(tap.parent / ("core-result.json" if core_only else "world-result.json"), report)
    if failures:
        raise CommandError("test inventory reconciliation failed", details={"suites": failures})
    return report


def verify_required_tests(
    feature: dict, report: dict, world: dict, branch="candidate"
) -> list[str]:
    from pg_maker.changes.loader import required_tests

    evidence = {item["id"] for item in report.get("acceptance", []) if item["passed"]}
    for component in report.get("components", []):
        if component["passed"]:
            evidence.update(
                component["id"] + "." + target for target in component["native_targets"]
            )
    for result in world["results"]:
        if not result["passed"]:
            continue
        suite = result["suite"]
        if suite["kind"] == "tap":
            if not result["observed"]["skip_all"]:
                evidence.add(suite["path"])
        else:
            evidence.update(suite["directory"] + "/" + name for name in suite["tests"])
    missing = sorted(set(required_tests(feature, branch)) - evidence)
    if missing:
        raise CommandError("required tests have no successful evidence", details={"tests": missing})
    return sorted(evidence)
