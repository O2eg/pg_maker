"""Prepare a pinned offline runtime repository inside the build image."""

from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.storage import content_hash, file_hash, parse_json

REPOSITORY = "/opt/pg-maker/runtime-repository"


def verify_repository(directory: Path, manifest: dict) -> list[dict]:
    """Check the complete flat repository before copying it or using trusted=yes."""
    from pg_maker.documents import relative_output

    files = manifest.get("files", [])
    if (
        not files
        or manifest.get("repository_hash") != content_hash(files)
        or any(p.is_symlink() for p in (directory, *directory.parents))
        or not directory.is_dir()
    ):
        raise PreconditionError("missing or invalid pinned runtime repository; rebuild packages")
    names = [f["path"] for f in files]
    if (
        len(names) != len(set(names))
        or "Packages" not in names
        or any(not relative_output(n) or "/" in n for n in names)
        or set(names) != {p.name for p in directory.iterdir()}
    ):
        raise PreconditionError("runtime repository inventory differs from prepared manifest")
    for item in files:
        path = directory / item["path"]
        if path.is_symlink() or not path.is_file() or file_hash(path) != item["sha256"]:
            raise PreconditionError("runtime repository differs from prepared manifest")
    return files


INVENTORY_SCRIPT = """
import hashlib,json,pathlib
root=pathlib.Path('/opt/pg-maker/runtime-repository')
files=[]
for p in sorted(root.glob('*')):
    if p.is_file() and (p.suffix=='.deb' or p.name=='Packages'):
        h=hashlib.sha256()
        with p.open('rb') as f:
            for b in iter(lambda:f.read(1048576),b''): h.update(b)
        files.append(dict(path=p.name,sha256='sha256:'+h.hexdigest(),size_bytes=p.stat().st_size))
print(json.dumps(files))
"""


def prepare_repository(execute, packages, *, offline=False):
    if not packages:
        return None
    if not offline:
        execute(
            [
                "sh",
                "-ec",
                "mkdir -p /opt/pg-maker/runtime-repository/partial; "
                ": > /opt/pg-maker/runtime-empty-status",
            ]
        )
        execute(
            [
                "apt-get",
                "-y",
                "--download-only",
                "--no-install-recommends",
                "-o",
                "Dir::State::status=/opt/pg-maker/runtime-empty-status",
                "-o",
                "Dir::Cache::archives=" + REPOSITORY,
                "install",
                *packages,
            ]
        )
        execute(
            [
                "sh",
                "-ec",
                "cd /opt/pg-maker/runtime-repository; "
                "dpkg-scanpackages --multiversion . /dev/null > Packages",
            ]
        )
    result = execute(["python3", "-c", INVENTORY_SCRIPT])
    files = parse_json(Path(result.stdout_log).read_text())
    if not files or not any(p["path"] == "Packages" for p in files):
        raise PreconditionError("offline runtime repository is missing; run env prepare online")
    return {"path": REPOSITORY, "files": files, "repository_hash": content_hash(files)}
