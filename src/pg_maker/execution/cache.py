"""Atomic file snapshots: private build checkpoints and immutable shared build outputs."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.storage import (
    content_hash,
    ensure_directory,
    exclusive_lock,
    file_hash,
    read_json,
    sync_directory,
    write_json,
)


class SnapshotStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, category: str, digest: str) -> Path:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValidationError("invalid snapshot address")
        return self.directory / category / digest[7:]

    def _put_file(self, path: Path, pending: set[Path]) -> str:
        digest = file_hash(path)
        destination = self._path("files", digest)
        ensure_directory(destination.parent)
        if (
            destination.is_file()
            and not destination.is_symlink()
            and file_hash(destination) == digest
        ):
            # It may be a valid but not yet synced orphan from an interrupted capture.
            pending.add(destination)
            return digest
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with path.open("rb") as incoming:
                    shutil.copyfileobj(incoming, stream)
                stream.flush()
                if file_hash(temporary) != digest:
                    raise PreconditionError("file changed during checkpoint")
                os.replace(temporary, destination)
                pending.add(destination)
            finally:
                temporary.unlink(missing_ok=True)
        return digest

    def capture(
        self,
        roots: dict[str, Path],
        metadata: dict,
        *,
        check=lambda: None,
        key: str | None = None,
        producer: dict | None = None,
        operation: Path | None = None,
    ) -> dict:
        ensure_directory(self.directory)
        entries = []
        with exclusive_lock(self.directory / ".lock"):
            write_json(self.directory / "store.json", {"format": "pg_maker/snapshot-store/v1"})
            pending = set()

            def visit(path, relative):
                check()
                mode = path.lstat().st_mode
                item = {"path": relative, "mode": stat.S_IMODE(mode)}
                if stat.S_ISDIR(mode):
                    item["type"] = "directory"
                    entries.append(item)
                    for child in sorted(path.iterdir()):
                        visit(child, relative + "/" + child.name)
                    return
                if stat.S_ISREG(mode):
                    item.update(
                        type="file",
                        sha256=self._put_file(path, pending),
                        size=path.stat().st_size,
                        mtime_ns=path.stat().st_mtime_ns,
                    )
                elif stat.S_ISLNK(mode):
                    # Checkpoint links are recreated, never followed while reading or restoring.
                    item.update(type="symlink", target=os.readlink(path))
                else:
                    raise PreconditionError(
                        "cannot checkpoint a special file", details={"path": relative}
                    )
                entries.append(item)

            for name, path in roots.items():
                if not relative_output(name) or "/" in name:
                    raise ValidationError("snapshot root must be a simple relative name")
                if path.is_symlink():
                    raise ValidationError("snapshot root cannot be a symlink")
                if path.exists():
                    visit(path, name)
            # Batch writes before syncing: a large source tree should not force a journal
            # commit for every rename. No snapshot/entry is visible until every byte and
            # the shared files directory are durable. Interrupted writes are unreferenced.
            for path in sorted(pending):
                check()
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
            if pending:
                sync_directory(self.directory / "files")
            core = {"format": "pg_maker/snapshot/v1", "entries": entries, "metadata": metadata}
            document = {**core, "snapshot_hash": content_hash(core)}
            write_json(self._path("snapshots", document["snapshot_hash"]), document)
            if key is not None:
                write_json(
                    self._path("entries", key),
                    {
                        "key": key,
                        "snapshot_hash": document["snapshot_hash"],
                        "producer": producer,
                    },
                )
                if operation is not None:
                    self._pin(operation, key)
        return document

    def verify(self, digest: str) -> dict:
        document = read_json(self._path("snapshots", digest))
        core = {k: v for k, v in document.items() if k != "snapshot_hash"}
        if document.get("snapshot_hash") != digest or content_hash(core) != digest:
            raise PreconditionError("snapshot manifest hash changed")
        seen = set()
        for entry in document["entries"]:
            path = entry["path"]
            if not relative_output(path) or path in seen:
                raise PreconditionError("invalid snapshot path")
            seen.add(path)
            if entry["type"] == "file":
                file = self._path("files", entry["sha256"])
                if file.is_symlink() or not file.is_file() or file_hash(file) != entry["sha256"]:
                    raise PreconditionError(
                        "snapshot file is missing or changed", details={"path": path}
                    )
                if file.stat().st_size != entry["size"]:
                    raise PreconditionError("snapshot file size changed")
            elif entry["type"] not in {"symlink", "directory"}:
                raise PreconditionError("unknown snapshot entry")
        return document

    def restore(self, digest: str, roots: dict[str, Path], *, check=lambda: None) -> dict:
        # Keep prune and publishers outside the verification/copy transaction.
        with exclusive_lock(self.directory / ".lock"):
            document = self.verify(digest)
            names = {e["path"].split("/", 1)[0] for e in document["entries"]}
            if names - roots.keys():
                raise PreconditionError("snapshot contains unexpected roots")
            for name in names:
                if roots[name].exists() or roots[name].is_symlink():
                    raise PreconditionError("restore requires fresh destination roots")
            for entry in document["entries"]:
                check()
                parts = Path(entry["path"]).parts
                path = roots[parts[0]].joinpath(*parts[1:])
                ensure_directory(path.parent)
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise PreconditionError("snapshot restore traverses a symlink")
                if entry["type"] == "directory":
                    path.mkdir()
                elif entry["type"] == "file":
                    shutil.copyfile(self._path("files", entry["sha256"]), path)
                else:
                    path.symlink_to(entry["target"])
                if entry["type"] != "symlink":
                    path.chmod(entry["mode"])
                    if entry["type"] == "file" and "mtime_ns" in entry:
                        os.utime(path, ns=(entry["mtime_ns"], entry["mtime_ns"]))
        return document

    def publish(self, key: str, snapshot: dict, producer: dict) -> None:
        with exclusive_lock(self.directory / ".lock"):
            self.verify(snapshot["snapshot_hash"])
            write_json(
                self._path("entries", key),
                {
                    "key": key,
                    "snapshot_hash": snapshot["snapshot_hash"],
                    "producer": producer,
                },
            )

    def lookup(self, key: str) -> dict | None:
        with exclusive_lock(self.directory / ".lock"):
            path = self._path("entries", key)
            if not path.is_file():
                return None
            entry = read_json(path)
            if entry.get("key") != key:
                raise PreconditionError("cache entry key differs")
            self.verify(entry["snapshot_hash"])
            return entry

    def pin(self, operation: Path, key: str) -> None:
        with exclusive_lock(self.directory / ".lock"):
            self._pin(operation, key)

    def _pin(self, operation, key):
        path = self._path("pins", content_hash(str(operation)))
        value = read_json(path) if path.is_file() else {"operation": str(operation), "keys": []}
        value["keys"] = sorted(set(value["keys"]) | {key})
        write_json(path, value)

    def unpin(self, operation: Path) -> None:
        """Caller must hold the inactive operation owner lock and retain its exported evidence."""
        with exclusive_lock(self.directory / ".lock"):
            self._path("pins", content_hash(str(operation))).unlink(missing_ok=True)

    def prune_plan(self) -> dict:
        ensure_directory(self.directory)
        with exclusive_lock(self.directory / ".lock"):
            return self._prune_plan()

    def _prune_plan(self) -> dict:
        # Pins are conservative retention roots, including interrupted runs. InputStore is
        # separate and is never pruned here; exported artifacts are independent copies.
        if (self.directory / "checkpoint-store").exists():
            raise PreconditionError("private recovery checkpoints cannot be cache-pruned")
        if not (self.directory / "store.json").is_file():
            raise PreconditionError("not a pg_maker cache store")
        pinned = {
            key for path in (self.directory / "pins").glob("*") for key in read_json(path)["keys"]
        }
        retained_snapshots, doomed = set(), []
        for path in (self.directory / "entries").glob("*"):
            entry = read_json(path)
            if entry["key"] in pinned:
                retained_snapshots.add(entry["snapshot_hash"])
            else:
                doomed.append(path)
        retained_files = set()
        for path in (self.directory / "snapshots").glob("*"):
            digest = "sha256:" + path.name
            if digest in retained_snapshots:
                document = self.verify(digest)
                retained_files.update(
                    e["sha256"] for e in document["entries"] if e["type"] == "file"
                )
            else:
                doomed.append(path)
        for path in (self.directory / "files").glob("*"):
            if "sha256:" + path.name not in retained_files:
                doomed.append(path)
        files = [
            {
                "path": p.relative_to(self.directory).as_posix(),
                "sha256": file_hash(p),
                "size_bytes": p.stat().st_size,
            }
            for p in sorted(doomed)
        ]
        core = {"format": "pg_maker/cache-prune/v1", "files": files}
        return {
            **core,
            "plan_hash": content_hash(core),
            "size_bytes": sum(f["size_bytes"] for f in files),
        }

    def prune(self, plan: dict) -> dict:
        with exclusive_lock(self.directory / ".lock"):
            current = self._prune_plan()
            if current != plan:
                raise PreconditionError(
                    "cache references or files changed; create a new prune plan"
                )
            for item in plan["files"]:
                path = self.directory / item["path"]
                if path.is_symlink():
                    raise PreconditionError("cache prune refuses symbolic links")
                path.unlink()
            sync_directory(self.directory)
            return {"removed": len(plan["files"]), "size_bytes": plan["size_bytes"]}
