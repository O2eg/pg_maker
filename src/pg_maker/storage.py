"""Strict JSON, durable replacement and Linux advisory locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pg_maker.errors import OwnershipError, ValidationError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    except (ValueError, TypeError) as exc:
        raise ValidationError("value is not canonical JSON") from exc


def content_hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def parse_json(payload: str | bytes) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"non-finite number: {value}")

    try:
        return json.loads(payload, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(f"invalid JSON: {exc}") from exc


def read_json(path: Path) -> Any:
    return parse_json(path.read_bytes())


def ensure_directory(path: Path) -> None:
    # Inspect the spelling before resolve(): resolving first would hide symlinks.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise OwnershipError(f"managed directory must not traverse a symlink: {item}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def private_open(path: Path, *, append: bool = False, exclusive: bool = False) -> int:
    ensure_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_APPEND if append else os.O_TRUNC
    if exclusive:
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_text(path: Path, value: str) -> None:
    ensure_directory(path.parent)
    if path.is_symlink():
        raise OwnershipError(f"refusing to replace a symbolic link: {path}")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    write_text(path, canonical_json(value) + "\n")


@contextmanager
def exclusive_lock(path: Path, *, blocking: bool = True) -> Iterator[int]:
    ensure_directory(path.parent)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise OwnershipError(f"resource is locked: {path}") from exc
        yield descriptor
    finally:
        # Closing, rather than LOCK_UN, also supports handoff via pass_fds to a worker.
        os.close(descriptor)
