"""One locked source-test policy for build, package and standalone test goals."""

from fnmatch import fnmatchcase

from pg_maker.builders.autotools import extra_tests
from pg_maker.errors import PreconditionError
from pg_maker.storage import write_json


def source_test_policy(config, build, goal, branch="candidate"):
    validation = config["defaults"]["validation"]
    suites = list(validation.get("test_suites", ["standard"]))
    if goal == "test-full" and "long" not in suites:
        suites.append("long")
    if branch == "baseline":
        suites = ["baseline-core"]
    recipe = config["postgresql_recipe"]
    extended = extra_tests(build, recipe, long_tests="long" in suites).split()
    patterns = {}
    if "long" in suites:
        for name in recipe.get("long_tests", []):
            evidence = recipe.get("long_test_evidence", {}).get(name)
            if not evidence:
                raise PreconditionError("long test has no evidence adapter", details={"test": name})
            patterns[name] = evidence
        if not patterns:
            raise PreconditionError("PostgreSQL recipe has no long test suite")
    return {
        "suites": sorted(suites),
        "extra_tests": extended,
        "long_test_evidence": patterns,
        "required_tests": validation.get("required_tests", []) if branch == "candidate" else [],
    }


def verify_source_test_policy(branch, inventory, world):
    policy = source_test_policy(
        branch.config, branch.variant["build"], branch.request.plan["spec"]["goal"], branch.branch
    )
    if inventory.get("extra_tests", "").split() != policy["extra_tests"]:
        raise PreconditionError("test inventory differs from the locked test policy")
    if not world.get("passed"):
        raise PreconditionError("source test policy lacks successful suite evidence")
    successful = {
        r["suite"].get("path")
        for r in world["results"]
        if r["passed"] and r["suite"]["kind"] == "tap" and not r["observed"]["skip_all"]
    }
    for name, patterns in policy["long_test_evidence"].items():
        for pattern in patterns:
            expected = {
                s["path"]
                for s in inventory["suites"]
                if s["kind"] == "tap" and fnmatchcase(s["path"], pattern)
            }
            if not expected or not expected <= successful:
                raise PreconditionError(
                    "required long test has no successful evidence",
                    details={"test": name, "pattern": pattern},
                )
    evidence = {
        "policy": policy,
        "passed": True,
        "lock_hash": branch.request.lock["lock_hash"],
        "plan_hash": branch.request.plan["plan_hash"],
    }
    write_json(branch.output / "source-test-policy.json", evidence)
    return evidence


def require_package_test_policy(branch):
    expected = source_test_policy(
        branch.config, branch.variant["build"], branch.request.plan["spec"]["goal"], branch.branch
    )
    evidence = branch.report.get("test_policy", {})
    if (
        not evidence.get("passed")
        or evidence.get("policy") != expected
        or evidence.get("lock_hash") != branch.request.lock["lock_hash"]
        or evidence.get("plan_hash") != branch.request.plan["plan_hash"]
    ):
        raise PreconditionError("package lacks evidence for the locked test policy")
