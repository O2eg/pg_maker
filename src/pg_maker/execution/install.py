"""Component DESTDIR publication with owned files and a recoverable commit intent."""

import os
import shutil
from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, read_json, sync_directory, write_json


def same(left, right):
    return {k: v for k, v in left.items() if k != "path"} == {
        k: v for k, v in right.items() if k != "path"
    }


def payload(root):
    return {e["path"]: e for e in inventory(root)["entries"] if e["type"] != "directory"}


def check_parents(root, name):
    path = root / name
    if not path.is_relative_to(root) or ".." in Path(name).parts:
        raise PreconditionError("invalid component install path")
    if any(p.is_symlink() for p in path.parents if p != root):
        raise PreconditionError("component installation traverses a symlink")
    return path


class ComponentInstall:
    def __init__(self, branch, identifier):
        self.branch, self.identifier = branch, identifier
        self.prefix = branch.root / "prefix"
        self.directory = branch.root / "install-staging" / identifier
        self.destination = self.directory / branch.prefix.lstrip("/")
        self.container = branch.container_root + "/install-staging/" + identifier
        self.record = branch.root / "install-owners" / (identifier + ".json")
        self.intent = branch.root / "install-owners" / (identifier + ".pending.json")

    def begin(self):
        self.recover()
        if self.directory.exists():
            shutil.rmtree(self.directory)
        self.destination.mkdir(parents=True)
        # Some PGXS install hooks assume the directories created by PostgreSQL
        # already exist. Mirror directories only, never the installed file tree.
        for parent, directories, _ in os.walk(self.prefix, followlinks=False):
            for name in directories:
                path = Path(parent) / name
                if not path.is_symlink():
                    (self.destination / path.relative_to(self.prefix)).mkdir(exist_ok=True)
        return self

    def recover(self):
        if self.intent.exists():
            document = read_json(self.intent)
            if content_hash(payload(self.destination)) != document["payload_hash"]:
                raise PreconditionError("interrupted component staging changed")
            self._publish(document)

    def commit(self):
        incoming = payload(self.destination)
        if not incoming:
            raise PreconditionError(
                "component installed no staged files; its installer must honor DESTDIR"
            )
        old = read_json(self.record).get("files", {}) if self.record.exists() else {}
        owned = {}
        for name, entry in incoming.items():
            target = check_parents(self.prefix, name)
            if target.exists() or target.is_symlink():
                # Core/contrib may already provide an identical file. It keeps ownership.
                current = payload_single(target, name)
                if name not in old:
                    if not same(current, entry):
                        raise PreconditionError(
                            "component install conflicts with existing files",
                            details={"path": name},
                        )
                    continue
            owned[name] = entry
        document = {
            "component": self.identifier,
            "files": owned,
            "remove": sorted(set(old) - set(owned)),
            "payload_hash": content_hash(incoming),
        }
        write_json(self.intent, document)
        self._publish(document)

    def _publish(self, document):
        directories = set()
        for name in document["remove"]:
            path = check_parents(self.prefix, name)
            path.unlink(missing_ok=True)
            if path.parent.exists():
                directories.add(path.parent)
        for name, entry in document["files"].items():
            target = check_parents(self.prefix, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".pg-maker-new")
            temporary.unlink(missing_ok=True)
            source = self.destination / name
            if entry["type"] == "symlink":
                temporary.symlink_to(entry["target"])
            else:
                shutil.copy2(source, temporary)
                with temporary.open("rb") as stream:
                    os.fsync(stream.fileno())
            os.replace(temporary, target)
            directories.add(target.parent)
        for directory in directories:
            for parent in (directory, *directory.parents):
                if parent.is_relative_to(self.prefix):
                    sync_directory(parent)
        write_json(self.record, {"component": self.identifier, "files": document["files"]})
        self.intent.unlink()
        sync_directory(self.intent.parent)
        shutil.rmtree(self.directory)


def payload_single(path, name):
    import stat

    from pg_maker.storage import file_hash

    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return {
            "path": name,
            "type": "symlink",
            "mode": stat.S_IMODE(mode),
            "target": os.readlink(path),
        }
    if not stat.S_ISREG(mode):
        raise PreconditionError("component target is not a regular file or symlink")
    return {"path": name, "type": "file", "mode": stat.S_IMODE(mode), "sha256": file_hash(path)}
