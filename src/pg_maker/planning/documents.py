"""Hashed planning documents keep transport and timestamps outside semantic identity."""

import hashlib
from pathlib import Path

from pg_maker.config import resource_path
from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError
from pg_maker.storage import content_hash, utc_now

KINDS = {"environment": "EnvironmentManifest", "lock": "BuildLock", "plan": "BuildPlan"}


def seal(name: str, spec: dict, transport: dict | None = None) -> dict:
    core = {"api_version": f"pg_maker/{name}/v1", "kind": KINDS[name], "spec": spec}
    result = {
        **core,
        name + "_hash": content_hash(core),
        "transport": transport or {},
        "created_at": utc_now(),
    }
    validate_document(result, name)
    return result


def verify(document: dict, name: str) -> None:
    validate_document(document, name)
    core = {key: document[key] for key in ("api_version", "kind", "spec")}
    if content_hash(core) != document[name + "_hash"]:
        raise PreconditionError(f"{name} hash mismatch")


def implementation_identity() -> dict:
    from pg_maker import __version__

    root = resource_path()
    inputs = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {
            ".py",
            ".json",
            ".yaml",
            ".patch",
            ".in",
            ".html",
            ".css",
            ".js",
            ".sgml",
        }:
            inputs.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    return {"version": __version__, "code_hash": content_hash(inputs)}


def store_from_document(document: dict, override: Path | None = None):
    from pg_maker.sources.store import InputStore

    path = override or Path(document["transport"].get("input_store", ""))
    if not path.is_absolute():
        raise PreconditionError("an absolute input-store location is required")
    return InputStore(path)
