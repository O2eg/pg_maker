"""Content-addressed inputs. No network access is performed by this storage layer."""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

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

TREE_FORMAT = "pg_maker/tree/v1"


def materialize(source: Path, destination: Path) -> None:
    """Copy immutable content with equal mtimes before applying any changes.

    Tree identity excludes mtimes. VPATH make rules (notably PG14's paired
    lwlocknames.c/h generator) must not observe incidental extraction order.
    Later ChangeSet edits still get new mtimes and trigger native generators.
    """
    shutil.copytree(source, destination, symlinks=True)
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink():
            os.utime(path, (1, 1))


def checked_link(path: str, target: str) -> None:
    if not target or PurePosixPath(target).is_absolute() or "\x00" in target:
        raise ValidationError(
            "source contains an absolute or invalid symlink", details={"path": path}
        )
    stack = list(PurePosixPath(path).parent.parts)
    for part in PurePosixPath(target).parts:
        if part == "..":
            if not stack:
                raise ValidationError("source symlink escapes its root", details={"path": path})
            stack.pop()
        elif part != ".":
            stack.append(part)


def inventory(root: Path) -> dict:
    entries = []

    def visit(directory: Path) -> None:
        for path in directory.iterdir():
            rel = path.relative_to(root).as_posix()
            try:
                rel.encode("utf-8")
            except UnicodeError as exc:
                raise ValidationError("source filenames must be UTF-8") from exc
            mode = path.lstat().st_mode
            item = {"path": rel, "mode": stat.S_IMODE(mode)}
            if stat.S_ISLNK(mode):
                link = os.readlink(path)
                checked_link(rel, link)
                # Also check composed links, not only the spelling of each link separately.
                try:
                    if not path.resolve().is_relative_to(root.resolve()):
                        raise ValidationError("source symlink chain escapes its root")
                except (RuntimeError, OSError) as exc:
                    raise ValidationError("source contains a symlink cycle") from exc
                item.update(type="symlink", target=link)
            elif stat.S_ISREG(mode):
                item.update(type="file", sha256=file_hash(path))
            elif stat.S_ISDIR(mode):
                item.update(type="directory")
                visit(path)
            else:
                raise ValidationError("source contains a special file", details={"path": rel})
            entries.append(item)

    visit(root)
    return {
        "format": TREE_FORMAT,
        "entries": sorted(entries, key=lambda item: item["path"].encode("utf-8")),
    }


def copy_snapshot(source: Path, destination: Path, include: list[str], exclude: list[str]) -> None:
    source = source.resolve()
    if not source.is_dir() or destination.resolve().is_relative_to(source):
        raise ValidationError("snapshot needs a directory outside the input store")
    if not include or any(
        not p or p.startswith("/") or ".." in PurePosixPath(p).parts for p in [*include, *exclude]
    ):
        raise ValidationError("snapshot filters must be explicit relative patterns")

    def matches(path, patterns):
        return any(
            fnmatch.fnmatchcase(path, p) or (p.endswith("/**") and path == p[:-3]) for p in patterns
        )

    def visit(directory: Path):
        for path in sorted(directory.iterdir()):
            rel = path.relative_to(source).as_posix()
            if matches(rel, exclude):
                continue
            target = destination / rel
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                # Empty selected directories are inputs too; traverse unselected parents.
                if matches(rel, include):
                    target.mkdir(parents=True, exist_ok=True)
                visit(path)
                if target.exists():
                    os.chmod(target, stat.S_IMODE(mode))
            elif matches(rel, include):
                target.parent.mkdir(parents=True, exist_ok=True)
                if stat.S_ISLNK(mode):
                    checked_link(rel, os.readlink(path))
                    target.symlink_to(os.readlink(path))
                elif stat.S_ISREG(mode):
                    shutil.copyfile(path, target, follow_symlinks=False)
                    os.chmod(target, stat.S_IMODE(mode))
                else:
                    raise ValidationError("snapshot contains a special file", details={"path": rel})

    visit(source)
    inventory(destination)


def extract_archive(archive: Path, destination: Path, strip_components: int = 0) -> None:
    """Extract ordinary files/directories and safe links; never use tar.extractall."""
    try:
        with tarfile.open(archive, "r:*") as stream:
            seen, links, directories = set(), [], []
            for member in stream:
                name = member.name.rstrip("/")
                if name in ("", "."):
                    continue
                if name.startswith("./"):
                    name = name[2:]
                if not relative_output(name):
                    raise ValidationError("archive contains a non-relative member")
                parts = PurePosixPath(name).parts[strip_components:]
                if not parts:
                    continue
                name = str(PurePosixPath(*parts))
                if name in seen:
                    raise ValidationError("archive contains duplicate members")
                seen.add(name)
                path = destination / name
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValidationError("archive writes through a symbolic link")
                path.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    path.mkdir(exist_ok=True)
                    directories.append((path, member.mode))
                elif member.isfile():
                    with stream.extractfile(member) as incoming, path.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
                    os.chmod(path, member.mode & 0o7777)
                elif member.issym():
                    checked_link(name, member.linkname)
                    path.symlink_to(member.linkname)
                elif member.islnk():
                    link = member.linkname.removeprefix("./")
                    if not relative_output(link):
                        raise ValidationError("invalid archive hardlink")
                    link_parts = PurePosixPath(link).parts[strip_components:]
                    if not link_parts:
                        raise ValidationError("hardlink target was stripped")
                    links.append((path, destination.joinpath(*link_parts)))
                else:
                    raise ValidationError("archive contains a special file")
            for path, target in links:
                if any(p.is_symlink() for p in (target, *target.parents)) or not target.is_file():
                    raise ValidationError("archive hardlink must target an extracted regular file")
                shutil.copyfile(target, path)
                shutil.copymode(target, path)
            for path, mode in reversed(directories):
                os.chmod(path, mode & 0o7777)
            inventory(destination)
    except (tarfile.TarError, OSError) as exc:
        raise ValidationError("cannot safely extract source archive") from exc


class InputStore:
    def __init__(self, root: Path):
        self.root = root.absolute()

    def path(self, kind: str, digest: str) -> Path:
        import re

        if kind not in {"blobs", "trees", "resolutions"} or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise ValidationError("invalid content address")
        return self.root / kind / digest[7:]

    def blob(self, digest: str) -> Path:
        path = self.path("blobs", digest)
        if not path.is_file() or path.is_symlink() or file_hash(path) != digest:
            raise PreconditionError("missing or changed input blob", details={"sha256": digest})
        return path

    def put_blob(self, source: Path, expected: str | None = None) -> str:
        digest = file_hash(source)
        if expected is not None and digest != expected:
            raise PreconditionError(
                "input checksum mismatch", details={"expected": expected, "actual": digest}
            )
        target = self.path("blobs", digest)
        ensure_directory(target.parent)
        with exclusive_lock(self.root / ".inputs.lock"):
            if target.exists():
                self.blob(digest)
            else:
                with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        with source.open("rb") as incoming:
                            shutil.copyfileobj(incoming, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                        if file_hash(temporary) != digest:
                            raise PreconditionError("input changed while being copied")
                        os.replace(temporary, target)
                        sync_directory(target.parent)
                    finally:
                        temporary.unlink(missing_ok=True)
        return digest

    def put_tree(self, source: Path) -> str:
        value = inventory(source)
        digest = content_hash(value)
        target = self.path("trees", digest)
        ensure_directory(target.parent)
        with exclusive_lock(self.root / ".inputs.lock"):
            if target.exists():
                self.tree(digest)
            else:
                with tempfile.TemporaryDirectory(dir=target.parent) as name:
                    temporary = Path(name) / "tree"
                    shutil.copytree(source, temporary, symlinks=True)
                    if inventory(temporary) != value:
                        raise PreconditionError("source changed while being copied")
                    for item in value["entries"]:
                        path = temporary / item["path"]
                        if item["type"] == "file":
                            with path.open("rb") as stream:
                                os.fsync(stream.fileno())
                        elif item["type"] == "directory":
                            sync_directory(path)
                    sync_directory(temporary)
                    os.replace(temporary, target)
                    sync_directory(target.parent)
                write_json(target.parent / (digest[7:] + ".json"), value)
        return digest

    def tree(self, digest: str) -> Path:
        path = self.path("trees", digest)
        if path.is_symlink() or not path.is_dir() or content_hash(inventory(path)) != digest:
            raise PreconditionError(
                "missing or changed source tree", details={"source_hash": digest}
            )
        return path

    def cached(self, key: str) -> dict | None:
        path = self.path("resolutions", key)
        return read_json(path) if path.is_file() else None

    def remember(self, key: str, value: dict) -> None:
        write_json(self.path("resolutions", key), value)
