"""Classify command diagnostics without mistaking negative SQL tests for failures."""

import re
import shutil
from collections import Counter
from pathlib import Path

from pg_maker.storage import write_json


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
            analysis = classify(target, None, test_command=True)
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


def classify(stdout, stderr, *, test_command=False, warnings_as_errors=False):
    findings = []
    for channel, path in (("stdout", stdout), ("stderr", stderr)):
        if path is None:
            continue
        with Path(path).open(errors="replace") as stream:
            for number, line in enumerate(stream, 1):
                category, blocking = None, False
                if SANITIZER.search(line):
                    category, blocking = "sanitizer", True
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
                if category:
                    findings.append(
                        {
                            "channel": channel,
                            "line": number,
                            "category": category,
                            "blocking": blocking,
                            "message": line.rstrip(),
                        }
                    )
    return {
        "passed": not any(f["blocking"] for f in findings),
        "test_command": test_command,
        "test_output_policy": "validated by suite results, not by ERROR/WARNING text alone",
        "counts": dict(Counter(f["category"] for f in findings)),
        "findings": findings,
    }


def record(branch, command, result, *, test_command=False):
    analysis = classify(
        result.stdout_log,
        result.stderr_log,
        test_command=test_command,
        warnings_as_errors=branch.config["defaults"]["validation"].get("warnings_as_errors", False),
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
