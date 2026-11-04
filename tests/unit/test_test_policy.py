from types import SimpleNamespace

import pytest

from pg_maker.builders.autotools import verify_required_tests
from pg_maker.errors import CommandError, PreconditionError
from pg_maker.postgresql import recipe
from pg_maker.validation.test_policy import (
    require_package_test_policy,
    source_test_policy,
    verify_source_test_policy,
)


def branch_fixture(tmp_path, version="17.5"):
    config = {
        "defaults": {"validation": {"test_suites": ["standard", "long"]}},
        "postgresql_recipe": recipe(version),
    }
    return SimpleNamespace(
        config=config,
        variant={"build": {"options": {"openssl": True}}},
        branch="candidate",
        output=tmp_path,
        report={},
        request=SimpleNamespace(
            plan={"spec": {"goal": "package"}, "plan_hash": "plan"},
            lock={"lock_hash": "lock"},
        ),
    )


def test_long_policy_requires_non_skipped_evidence_for_every_declared_suite(tmp_path):
    branch = branch_fixture(tmp_path)
    policy = source_test_policy(branch.config, branch.variant["build"], "package")
    paths = [
        "src/test/recovery/t/027_stream_regress.pl",
        "src/test/modules/xid_wraparound/t/001_emergency_vacuum.pl",
        "src/test/modules/xid_wraparound/t/002_limits.pl",
    ]
    inventory = {
        "extra_tests": " ".join(policy["extra_tests"]),
        "suites": [{"kind": "tap", "path": p} for p in paths],
    }
    world = {
        "passed": True,
        "results": [
            {"suite": suite, "passed": True, "observed": {"skip_all": ""}}
            for suite in inventory["suites"]
        ],
    }
    branch.report["test_policy"] = verify_source_test_policy(branch, inventory, world)
    require_package_test_policy(branch)
    world["results"][-1]["observed"]["skip_all"] = "disabled"
    with pytest.raises(PreconditionError, match="long test"):
        verify_source_test_policy(branch, inventory, world)
    world["results"].pop()
    with pytest.raises(PreconditionError, match="long test"):
        verify_source_test_policy(branch, inventory, world)
    inventory["suites"] = inventory["suites"][:1]
    with pytest.raises(PreconditionError, match="long test"):
        verify_source_test_policy(branch, inventory, world)


def test_package_gate_rejects_evidence_from_different_policy_lock_or_plan(tmp_path):
    branch = branch_fixture(tmp_path)
    policy = source_test_policy(branch.config, branch.variant["build"], "package")
    evidence = {"passed": True, "policy": policy, "lock_hash": "lock", "plan_hash": "plan"}
    for key, value in (
        ("passed", False),
        ("policy", {}),
        ("lock_hash", "other"),
        ("plan_hash", "other"),
    ):
        branch.report["test_policy"] = {**evidence, key: value}
        with pytest.raises(PreconditionError, match="locked test policy"):
            require_package_test_policy(branch)
    branch.report["test_policy"] = evidence
    require_package_test_policy(branch)
    with pytest.raises(PreconditionError, match="inventory differs"):
        verify_source_test_policy(branch, {"extra_tests": "ssl"}, {"passed": True})


def test_recipe_required_tests_cannot_be_satisfied_by_unexecuted_ids():
    feature = {"spec": {"required_tests": [], "changes": [], "components": []}}
    world = {"passed": True, "results": []}
    with pytest.raises(CommandError, match="no successful evidence"):
        verify_required_tests(feature, {}, world, additional=["external.check"])
    report = {"components": [{"id": "external", "passed": True, "native_targets": ["check"]}]}
    assert "external.check" in verify_required_tests(
        feature, report, world, additional=["external.check"]
    )
