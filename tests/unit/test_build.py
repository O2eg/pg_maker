"""Failure-oriented checks for source changes, inventory and locked execution."""

import pytest

from pg_maker.builders.autotools import reconcile, schedule_tests
from pg_maker.changes.apply import apply_patch, insert_text
from pg_maker.errors import CommandError, PreconditionError, UnsupportedError, ValidationError
from pg_maker.storage import file_hash, write_json


def insertion(path="code.c", **overrides):
    result = {
        "target_file": path,
        "input_sha256": None,
        "modifications": [
            {"id": "one", "top": "BEGIN", "bot": "END", "alignment": "bot", "txt": "new();"}
        ],
    }
    result.update(overrides)
    return result


def test_insert_exact_atomic_and_not_idempotent(tmp_path):
    path = tmp_path / "code.c"
    path.write_text("BEGIN\nold();\nEND\n")
    document = insertion(input_sha256=file_hash(path))
    insert_text(tmp_path, document)
    assert path.read_text() == "BEGIN\nold();\nnew();\nEND\n"
    with pytest.raises(PreconditionError, match="hash"):
        insert_text(tmp_path, document)
    with pytest.raises(PreconditionError, match="already present"):
        insert_text(tmp_path, insertion())
    document = insertion()
    document["modifications"][0]["txt"] = "another();"
    document["modifications"].append({**document["modifications"][0], "top": "missing"})
    before = path.read_bytes()
    with pytest.raises(PreconditionError):
        insert_text(tmp_path, document)
    assert path.read_bytes() == before


@pytest.mark.parametrize("text", ["BEGIN\nEND\nBEGIN\nEND\n", "END\nBEGIN\n"])
def test_insert_rejects_ambiguous_or_reversed_anchors(tmp_path, text):
    (tmp_path / "code.c").write_text(text)
    with pytest.raises(PreconditionError):
        insert_text(tmp_path, insertion())
    assert (tmp_path / "code.c").read_text() == text


def test_patch_exact_positions_and_add_delete(tmp_path):
    path = tmp_path / "code.c"
    path.write_text("a\nb\nc\n")
    apply_patch(tmp_path, "--- a/code.c\n+++ b/code.c\n@@ -2 +2 @@\n-b\n+B\n")
    assert path.read_text() == "a\nB\nc\n"
    with pytest.raises(PreconditionError):
        apply_patch(tmp_path, "--- a/code.c\n+++ b/code.c\n@@ -1 +1 @@\n-B\n+bad\n")
    apply_patch(tmp_path, "--- /dev/null\n+++ b/new\n@@ -0,0 +1 @@\n+hello\n")
    apply_patch(tmp_path, "--- a/new\n+++ /dev/null\n@@ -1 +0,0 @@\n-hello\n")
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("name", ["../../outside", "/outside", "link"])
def test_changes_reject_escape_or_symlink(tmp_path, name):
    (tmp_path / "link").symlink_to("/etc/passwd")
    with pytest.raises(ValidationError):
        insert_text(tmp_path, insertion(name))
    with pytest.raises(ValidationError):
        apply_patch(tmp_path, f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-x\n+y\n")


def test_patch_rejects_unsupported_modes_and_renames(tmp_path):
    with pytest.raises(UnsupportedError):
        apply_patch(tmp_path, "diff --git a/x b/x\nold mode 100644\nnew mode 100755\n")
    with pytest.raises(UnsupportedError):
        apply_patch(tmp_path, "--- a/x\n+++ b/y\n@@ -1 +1 @@\n-x\n+y\n")


def test_inventory_refuses_missing_empty_failed_or_extra_regressions(tmp_path):
    obj = tmp_path / "obj"
    (obj / "src/test/regress").mkdir(parents=True)
    tap = tmp_path / "results/tap"
    tap.mkdir(parents=True)
    inv = {
        "suites": [{"kind": "regression", "directory": "src/test/regress", "tests": ["one", "two"]}]
    }
    report = obj / "src/test/regress/regression.out"
    for text in (
        "",
        "ok 1 - one\n",
        "ok 1 - one\nnot ok 2 + two\n",
        "ok 1 - one\nok 2 + two\nok 3 - extra\n",
    ):
        report.write_text(text)
        with pytest.raises(CommandError):
            reconcile(inv, obj, tap)
    report.write_text("ok 1 - one\nok 2 + two\n")
    assert reconcile(inv, obj, tap)["passed"]
    (report.parent / "regression.diffs").write_text("unexpected output")
    with pytest.raises(CommandError):
        reconcile(inv, obj, tap)


def test_inventory_rejects_unknown_skip_and_accepts_declared_skip(tmp_path):
    tap = tmp_path / "world/tap"
    tap.mkdir(parents=True)
    inv = {"suites": [{"kind": "tap", "path": "suite/t/001.pl", "allowed_skip": None}]}
    result = {
        "path": "suite/t/001.pl",
        "tests": 0,
        "planned": 0,
        "failed": [],
        "errors": [],
        "skipped": [],
        "exit": 0,
        "wait": 0,
        "skip_all": "disabled",
    }
    write_json(tap / "one.json", result)
    with pytest.raises(CommandError):
        reconcile(inv, tmp_path, tap)
    inv["suites"][0]["allowed_skip"] = "disabled"
    assert reconcile(inv, tmp_path, tap)["passed"]
    result.update(tests=1, planned=2, skip_all="")
    write_json(tap / "one.json", result)
    with pytest.raises(CommandError):
        reconcile(inv, tmp_path, tap)


def test_required_schedule_is_nonempty_and_unique(tmp_path):
    schedule = tmp_path / "schedule"
    for text in ("# comment\n", "test: one one\n"):
        schedule.write_text(text)
        with pytest.raises(PreconditionError):
            schedule_tests(schedule)
    schedule.write_text("test: one two\ntest: three\n")
    assert schedule_tests(schedule) == ["one", "two", "three"]


def test_regression_stream_evidence_uses_suite_boundaries():
    from pg_maker.builders.autotools import regression_evidence

    output = """make[1]: Entering directory '/work/build/v.candidate/obj/src/test/regress'
ok 1    - one    12 ms
ok 2    + two    25 ms
1..2
make[1]: Leaving directory '/work/build/v.candidate/obj/src/test/regress'
make[2]: Entering directory '/work/build/v.candidate/obj/contrib/foo'
# +++ isolation check in contrib/foo +++
ok 1    - locks    20 ms
1..1
make[2]: Leaving directory '/work/build/v.candidate/obj/contrib/foo'
"""
    result = regression_evidence(output)
    assert result["regression", "src/test/regress"] == {
        "tests": ["one", "two"],
        "failed": [],
        "plans": [2],
    }
    assert result["isolation", "contrib/foo"]["tests"] == ["locks"]


def test_failed_changeset_keeps_original_tree(tmp_path):
    from pg_maker.changes.apply import apply_changes
    from pg_maker.sources.store import InputStore

    source = tmp_path / "source"
    source.mkdir()
    (source / "x").write_text("original\n")
    good = tmp_path / "good.patch"
    good.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-original\n+changed\n")
    bad = tmp_path / "bad.patch"
    bad.write_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-original\n+wrong\n")
    store = InputStore(tmp_path / "inputs")
    change = {
        "metadata": {"id": "test"},
        "spec": {
            "operations": [
                {"kind": "patch", "file": {"sha256": store.put_blob(p)}} for p in (good, bad)
            ]
        },
    }
    feature = {"spec": {"changes": [change]}}
    lock = {"spec": {"config": {"defaults": {"common_changes": []}}}}
    with pytest.raises(PreconditionError):
        apply_changes(source, feature, lock, store, "candidate", tmp_path / "receipts")
    assert (source / "x").read_text() == "original\n"
    assert not list((tmp_path / "receipts").glob("*.json"))


def test_component_empty_or_missing_regression_is_not_success(tmp_path):
    from pg_maker.builders.components import verify_component_tests

    expected = {"regression": ["probe"], "isolation": [], "tap": []}
    with pytest.raises(CommandError):
        verify_component_tests(expected, "", tmp_path)
    verify_component_tests(expected, "ok 1 - probe 1 ms\n1..1\n", tmp_path)
    with pytest.raises(CommandError):
        verify_component_tests(expected, "ok 1 - probe 1 ms\n1..2\n", tmp_path)


def test_required_tests_cannot_be_silently_ignored():
    from pg_maker.builders.autotools import verify_required_tests

    feature = {"spec": {"changes": [], "components": [], "required_tests": ["missing-test"]}}
    with pytest.raises(CommandError, match="no successful evidence"):
        verify_required_tests(feature, {}, {"results": []})


def test_all_assertions_skipped_is_not_a_successful_suite(tmp_path):
    tap = tmp_path / "world/tap"
    tap.mkdir(parents=True)
    inv = {"suites": [{"kind": "tap", "path": "t/001.pl", "allowed_skip": None}]}
    write_json(
        tap / "one.json",
        {
            "path": "t/001.pl",
            "tests": 2,
            "planned": 2,
            "failed": [],
            "errors": [],
            "skipped": [1, 2],
            "exit": 0,
            "wait": 0,
            "skip_all": "",
        },
    )
    with pytest.raises(CommandError):
        reconcile(inv, tmp_path, tap)


def test_cluster_cleanup_preserves_original_test_failure(tmp_path):
    from types import SimpleNamespace

    from pg_maker.builders.components import cluster

    def command(argv, **_kwargs):
        if argv[0].endswith("/initdb"):
            data = tmp_path / "clusters/probe/data"
            data.mkdir()
            (data / "postgresql.conf").write_text("")
        if argv[-1] == "stop":
            raise PreconditionError("container already stopped after timeout")

    branch = SimpleNamespace(
        root=tmp_path,
        container_root="/work/build/fixture",
        prefix="/opt/fixture",
        command=command,
        context=SimpleNamespace(token=SimpleNamespace(requested=lambda: False)),
    )
    original = CommandError("test timed out", details={"timed_out": True})
    with pytest.raises(CommandError) as raised:
        with cluster(branch, "probe"):
            raise original
    assert raised.value is original


def test_fast_component_build_installs_without_inventory_or_cluster(tmp_path):
    from types import SimpleNamespace

    from pg_maker.builders.components import build_components

    source = tmp_path / "input"
    source.mkdir()
    (source / "Makefile").write_text("all install:\n\ttrue\n")
    commands = []
    branch = SimpleNamespace(
        root=tmp_path / "work",
        container_root="/work/build/fixture",
        prefix="/opt/fixture",
        inputs=SimpleNamespace(tree=lambda digest: source),
        variant={"build": {"jobs": 1}},
        report={},
        command=lambda argv, **kw: commands.append(argv),
    )
    component = {
        "metadata": {"id": "probe"},
        "spec": {
            "type": "pgxs",
            "source": {"source_hash": "fixture"},
            "make_targets": ["all"],
            "install_targets": ["install"],
            "test_targets": ["installcheck"],
            "sql_extension": "probe",
        },
    }
    build_components(branch, [component], run_tests=False)
    assert len(commands) == 2
    assert [c[-1] for c in commands] == ["all", "install"]
    assert all(c[0] == "make" and "PG_CONFIG=/opt/fixture/bin/pg_config" in c for c in commands)
    report = branch.report["components"][0]
    assert report["built"] and report["tests"] == "skipped"
    assert "passed" not in report and not report["native_targets"]
