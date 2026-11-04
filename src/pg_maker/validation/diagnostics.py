"""Classify command diagnostics without mistaking negative SQL tests for failures."""

import re
import shutil
from collections import Counter
from fnmatch import fnmatchcase
from pathlib import Path

from pg_maker.errors import ValidationError
from pg_maker.storage import write_json


def compile_rules(rules):
    """Validate once during configuration, and again for standalone policy use."""
    compiled, seen = [], set()
    for rule in rules:
        identifier = rule["id"]
        if identifier in seen:
            raise ValidationError("duplicate diagnostic rule id", details={"id": identifier})
        seen.add(identifier)
        if "tools" in rule and "test-log" in rule.get("contexts", ["build", "test", "test-log"]):
            raise ValidationError(
                "tool-scoped diagnostic rules require explicit build/test contexts; "
                "generated test logs have no reliable executable identity",
                details={"id": identifier},
            )
        try:
            pattern = re.compile(rule["pattern"])
        except re.error as exc:
            raise ValidationError(
                "invalid diagnostic regular expression", details={"id": identifier}
            ) from exc
        compiled.append((rule, pattern))
    return compiled


def policy_from_config(config):
    validation = config.get("defaults", {}).get("validation", {})
    return {
        "warnings_as_errors": validation.get("warnings_as_errors", False),
        **validation.get("diagnostics", {}),
    }


def preserve_test_logs(branch, cwd):
    """Retain generated test evidence, including failed commands, outside mutable work trees."""
    relative = cwd.removeprefix(branch.container_root + "/")
    roots = [(branch.root / relative).resolve(), (branch.root / "clusters").resolve()]
    destination = branch.output / "test-logs" / branch.step["id"]
    copied = []
    findings = []
    for root in roots:
        if not root.is_dir() or not root.is_relative_to(branch.root):
            continue
        for path in root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if not (
                path.suffix == ".log"
                or path.name in {"regression.out", "regression.diffs", "Test.xml"}
            ):
                continue
            target = destination / path.relative_to(branch.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            copied.append(target.relative_to(branch.output).as_posix())
            analysis = classify(
                target,
                None,
                test_command=True,
                generated_log=True,
                policy=policy_from_config(getattr(branch, "config", {})),
                stage=branch.step.get("kind", ""),
                log_root=branch.output,
            )
            if analysis["findings"]:
                findings.append({"path": copied[-1], **analysis})
    write_json(destination / "index.json", {"files": copied})
    passed = all(f["passed"] for f in findings)
    write_json(destination / "diagnostics.json", {"passed": passed, "logs": findings})
    return passed


WARNING = re.compile(r"\bwarning\s*:|^CMake (?:Deprecation )?Warning\b|\*WARNING\*", re.I)
COMPILER_ERROR = re.compile(r":\d+(?::\d+)?:\s+(?:fatal\s+)?error:|undefined reference to")
SANITIZER = re.compile(
    r"(?:ERROR|SUMMARY): (?:AddressSanitizer|LeakSanitizer|ThreadSanitizer)"
    r"|UndefinedBehaviorSanitizer|:\d+:\d+: runtime error:"
)
DANGEROUS_WARNING = re.compile(
    r"implicit declaration of function|incompatible pointer type"
    r"|cast (?:to|from) pointer from integer of different size"
    r"|array subscript .*outside array bounds|use-after-free",
    re.I,
)
SERVER_ERROR = re.compile(r"\b(?:ERROR|FATAL|PANIC):")
CONFIGURE_ERROR = re.compile(r"(?:^|[/\s])configure(?:\.\w+)?:\s*(?:error|fatal):", re.I)
BUILD_PERMISSION = re.compile(r"\bPermission denied\b")
SHELL_ERROR = re.compile(
    r"^(?:/\S*/)?(?:sh|bash|dash|env)(?::|\s).*"
    r"(?:Permission denied|(?:command )?not found|No such file or directory)"
    r"|^make(?:\[\d+\])?:.*(?:\*\*\*|not found|Permission denied)"
)
TEST_DISABLED = re.compile(r'TAP tests not enabled|"make check" is not supported')


def classify(
    stdout,
    stderr,
    *,
    test_command=False,
    warnings_as_errors=False,
    policy=None,
    stage="",
    tool="",
    log_root=None,
    generated_log=False,
):
    policy = policy or {}
    warnings_as_errors = policy.get("warnings_as_errors", warnings_as_errors)
    rules = compile_rules(policy.get("rules", []))
    context = "test-log" if generated_log else "test" if test_command else "build"
    findings = []
    for channel, path in (("stdout", stdout), ("stderr", stderr)):
        if path is None:
            continue
        with Path(path).open(errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                category, blocking = None, False
                if SANITIZER.search(line):
                    category, blocking = "sanitizer", True
                elif not generated_log and (
                    CONFIGURE_ERROR.search(line) or SHELL_ERROR.search(line)
                ):
                    category, blocking = "tool-error", True
                elif not test_command and BUILD_PERMISSION.search(line):
                    category, blocking = "permission-error", True
                elif test_command and not generated_log and TEST_DISABLED.search(line):
                    category, blocking = "disabled-tests", True
                elif COMPILER_ERROR.search(line):
                    category, blocking = "compiler-error", not test_command
                elif WARNING.search(line):
                    category = "test-warning" if test_command else "build-warning"
                    blocking = not test_command and (
                        warnings_as_errors or bool(DANGEROUS_WARNING.search(line))
                    )
                elif SERVER_ERROR.search(line):
                    category = "test-diagnostic" if test_command else "runtime-error"
                    blocking = not test_command
                severity = (
                    "error"
                    if blocking
                    else "warning"
                    if category and "warning" in category
                    else "info"
                )
                matched, reason = None, None
                # Later explicit rules override earlier matches; keep ignored findings as evidence.
                for rule, pattern in rules:
                    if (
                        any(
                            key in rule and not any(fnmatchcase(value, p) for p in rule[key])
                            for key, value in (("stages", stage), ("tools", Path(tool).name))
                        )
                        or context not in rule.get("contexts", ["build", "test", "test-log"])
                        or channel not in rule.get("channels", ["stdout", "stderr"])
                        or not pattern.search(line)
                    ):
                        continue
                    category, matched = "rule:" + rule["id"], rule["id"]
                    severity, reason = rule["severity"], rule.get("reason")
                    blocking = severity == "error" or (
                        severity == "warning" and warnings_as_errors and not test_command
                    )
                if category:
                    file = Path(path)
                    if log_root is not None:
                        file = file.relative_to(log_root)
                    findings.append(
                        {
                            "file": str(file),
                            "channel": channel,
                            "line": number,
                            "category": category,
                            "blocking": blocking,
                            "message": line.rstrip(),
                            "severity": severity,
                            "rule": matched,
                            "reason": reason,
                        }
                    )
    return {
        "passed": not any(f["blocking"] for f in findings),
        "test_command": test_command,
        "context": context,
        "stage": stage,
        "tool": tool,
        "policy": policy,
        "test_output_policy": "validated by suite results, not by ERROR/WARNING text alone",
        "counts": dict(Counter(f["category"] for f in findings)),
        "findings": findings,
    }


def record(branch, command, result, *, test_command=False):
    analysis = classify(
        branch.output / f"command-logs/{branch.counter}.stdout.log",
        branch.output / f"command-logs/{branch.counter}.stderr.log",
        test_command=test_command,
        policy=policy_from_config(branch.config),
        stage=branch.step["kind"],
        tool=command.argv[0],
        log_root=branch.output,
    )
    analysis.update(command_id=command.id, step_id=branch.step["id"])
    path = "diagnostics/" + str(branch.counter) + ".json"
    write_json(branch.output / path, analysis)
    summary = branch.report.setdefault("diagnostics", {"commands": 0, "counts": {}, "failed": []})
    summary["commands"] += 1
    for name, count in analysis["counts"].items():
        summary["counts"][name] = summary["counts"].get(name, 0) + count
    if not analysis["passed"]:
        summary["failed"].append(path)
    return analysis
