"""Map validated documentation to the common InstallTree, independent of packager."""

import shutil
from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, read_json, write_json


def collect_docs(branch, staging, roles):
    document = read_json(branch.output / "docs-manifest.json")
    core = {key: value for key, value in document.items() if key != "manifest_hash"}
    source = branch.output / "docs"
    if (
        content_hash(core) != document["manifest_hash"]
        or inventory(source)["entries"] != document["entries"]
    ):
        raise PreconditionError("documentation changed before InstallTree collection")
    prefix = branch.prefix.lstrip("/")
    doc_root = branch.variant["identity"]["paths"]["documentation_directory"].lstrip("/")
    installed, excluded = [], []
    for entry in document["entries"]:
        if entry["type"] == "directory":
            continue
        parts = Path(entry["path"]).parts
        language, fmt = parts[:2]
        if fmt == "man":
            section = parts[2]
            name = Path(parts[-1]).stem
            if section == "man1":
                role = roles.get(prefix + "/bin/" + name)
                if role is None:
                    excluded.append({"path": entry["path"], "reason": "command-not-delivered"})
                    continue
            elif section == "man3":
                # Upstream man3 also contains backend SPI and SQL extension APIs.
                # Only libpq's PQ* interface belongs with the client library.
                role = "libpq" if name.startswith("PQ") else "server"
            else:
                role = "server"
            target = (
                prefix
                + "/share/man/"
                + (language + "/" if language != "en" else "")
                + "/".join(parts[2:])
            )
        else:
            role, target = "docs", doc_root + "/" + entry["path"]
        if target in roles:
            raise PreconditionError("documentation path collides with a runtime file")
        destination = staging / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        if entry["type"] == "symlink":
            destination.symlink_to(entry["target"])
        else:
            shutil.copyfile(source / entry["path"], destination)
            destination.chmod(0o644)
        roles[target] = role
        installed.append(
            {
                "path": target,
                "role": role,
                "language": language,
                "format": fmt,
                "source": entry["path"],
            }
        )
    write_json(
        branch.output / "installed-docs.json",
        {
            "manifest_hash": document["manifest_hash"],
            "files": installed,
            "excluded": excluded,
            "context": document["context"],
            "languages": document["profile"]["languages"],
            "formats": document["profile"]["formats"],
        },
    )
