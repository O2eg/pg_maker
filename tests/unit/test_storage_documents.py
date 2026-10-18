import io
import math

import pytest

from pg_maker.assets import initialize_project
from pg_maker.documents import validate_document
from pg_maker.errors import OwnershipError, ValidationError
from pg_maker.progress import ProgressReporter
from pg_maker.security import StreamRedactor
from pg_maker.storage import (
    canonical_json,
    content_hash,
    exclusive_lock,
    parse_json,
    read_json,
    write_json,
)


@pytest.mark.parametrize("payload", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_strict_json(payload):
    with pytest.raises(ValidationError):
        parse_json(payload)


def test_canonical_json_and_atomic_permissions(tmp_path):
    assert content_hash({"x": 1, "y": 2}) == content_hash({"y": 2, "x": 1})
    with pytest.raises(ValidationError):
        canonical_json({"x": math.nan})
    path = tmp_path / "state.json"
    write_json(path, {"x": 1})
    write_json(path, {"x": 2})
    assert read_json(path) == {"x": 2}
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".state.json.*"))


def test_io_refuses_symlink_and_lock_conflict(tmp_path):
    target = tmp_path / "untouched"
    target.write_text("original")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OwnershipError):
        write_json(link, {})
    assert target.read_text() == "original"
    with exclusive_lock(tmp_path / "owner.lock"):
        with pytest.raises(OwnershipError):
            with exclusive_lock(tmp_path / "owner.lock", blocking=False):
                pass


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(unknown=True),
        lambda d: d["commands"][0].update(timeout_seconds=True),
        lambda d: d["commands"][0].update(timeout_seconds=1.0),
        lambda d: d["commands"][0].update(cwd="/work/build/../../etc"),
        lambda d: d["commands"][0].update(env={"API_TOKEN": "do-not-persist"}),
        lambda d: d["commands"].append(d["commands"][0]),
        lambda d: d.update(outputs=["../escape"]),
        lambda d: d["environment"].update(image="postgres:18"),
    ],
)
def test_invalid_inputs(request_document, mutation):
    mutation(request_document)
    with pytest.raises(ValidationError):
        validate_document(request_document, "operation-input")


def test_init_idempotence_conflict_and_package_resources(tmp_path):
    result = initialize_project(tmp_path)
    assert "schemas/operation-input.schema.json" in result["created"]
    assert not initialize_project(tmp_path)["created"]
    (tmp_path / "README.md").write_text("user changes")
    (tmp_path / "schemas/operation-input.schema.json").unlink()
    with pytest.raises(OwnershipError):
        initialize_project(tmp_path)
    assert not (tmp_path / "schemas/operation-input.schema.json").exists()


def test_init_checks_parent_files_before_writing(tmp_path):
    (tmp_path / "schemas").write_text("user file")
    with pytest.raises(OwnershipError):
        initialize_project(tmp_path)
    assert not (tmp_path / "README.md").exists()


def test_unknown_field_pointer_is_precise(request_document):
    request_document["environment"]["future_field"] = "value"
    with pytest.raises(ValidationError) as error:
        validate_document(request_document, "operation-input")
    assert error.value.details["pointer"] == "/environment/future_field"


def test_progress_append_context_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_TOKEN", "sensitive-value")
    path = tmp_path / "progress.log"
    stream = io.StringIO()
    for _ in range(2):
        with ProgressReporter(path, operation_id="op1", target_id="target", stream=stream) as log:
            log.info("sensitive-value\nmore", step_id="build")
    assert "sensitive-value" not in path.read_text()
    assert len(path.read_text().splitlines()) == 2
    assert "operation=op1 target=target step=build attempt=1" in stream.getvalue()


@pytest.mark.parametrize("chunk_size", range(1, 32))
def test_redaction_across_every_chunk_boundary(chunk_size):
    payload = "Привет / abc-secret-value / abc-secret / end"
    redactor = StreamRedactor(("abc-secret-value", "abc-secret"))
    result = "".join(
        redactor.feed(payload[i : i + chunk_size]) for i in range(0, len(payload), chunk_size)
    )
    result += redactor.feed("", final=True)
    assert result == "Привет / [REDACTED] / [REDACTED] / end"
