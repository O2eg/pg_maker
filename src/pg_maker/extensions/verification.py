"""Verify exported extension package bytes and their complete evidence inventory."""

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError
from pg_maker.storage import content_hash, file_hash, read_json


def verify_extension_output(root):
    manifest = read_json(root / "extension-manifest.json")
    if manifest.get("manifest_hash") != content_hash(
        {k: v for k, v in manifest.items() if k != "manifest_hash"}
    ):
        raise PreconditionError("extension artifact manifest changed")
    paths = [record["path"] for record in manifest["evidence"]]
    actual = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "extension-manifest.json"
    }
    if (
        len(paths) != len(set(paths))
        or set(paths) != actual
        or any(p.is_symlink() for p in root.rglob("*"))
    ):
        raise PreconditionError("extension evidence inventory changed")
    for record in manifest["evidence"]:
        path = root / record["path"]
        if (
            not relative_output(record["path"])
            or not path.is_file()
            or any(p.is_symlink() for p in (path, *path.parents))
            or file_hash(path) != record["sha256"]
        ):
            raise PreconditionError("extension artifact or evidence changed")
