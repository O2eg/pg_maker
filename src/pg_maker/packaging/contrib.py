"""Inventory upstream contrib independently of the selected external components."""

import re
import shutil
from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.sources.store import inventory
from pg_maker.storage import read_json, write_json

REQUIRED_OPTIONS = ("openssl", "libxml", "libxslt", "uuid", "selinux", "perl", "python")


def enabled(config):
    return config["defaults"].get("packaging", {}).get("standard_contrib", False)


def capture(branch, env):
    """Use upstream's install target in an empty DESTDIR, before external installs."""
    helper = branch.helper / "contrib-inventory.mk"
    helper.write_text(
        ".PHONY: maker-contrib-inventory\n"
        "maker-contrib-inventory:\n"
        "\t@$(info PG_MAKER_CONTRIB=$(SUBDIRS))true\n"
    )
    directory = branch.container_root + "/obj/contrib"
    result = branch.command(
        [
            "make",
            "-s",
            "-C",
            directory,
            "-f",
            branch.container_root + "/source/contrib/Makefile",
            "-f",
            "/work/source/contrib-inventory.mk",
            "maker-contrib-inventory",
        ],
        env=env,
    )
    matches = re.findall(r"^PG_MAKER_CONTRIB=(.*)$", Path(result.stdout_log).read_text(), re.M)
    if len(matches) != 1:
        raise PreconditionError("cannot determine configured contrib modules")
    modules = set(matches[0].split())
    expected = {
        p.name
        for p in (branch.source / "contrib").iterdir()
        if p.is_dir() and (p / "Makefile").is_file()
    }
    if not expected or modules != expected:
        raise PreconditionError(
            "standard contrib configuration omits upstream modules",
            details={"missing": sorted(expected - modules), "extra": sorted(modules - expected)},
        )
    staging = branch.root / "contrib-stage"
    staging.mkdir()  # A stale or nonempty stage is never accepted as evidence.
    try:
        branch.command(
            [
                "make",
                "-C",
                directory,
                "-f",
                branch.container_root + "/source/contrib/Makefile",
                "install",
                "DESTDIR=" + branch.container_root + "/contrib-stage",
            ],
            env=env,
        )
        document = inventory(staging / branch.prefix.lstrip("/"))
        if not any(e["path"].endswith("/auto_explain.so") for e in document["entries"]):
            raise PreconditionError("standard contrib install lacks auto_explain")
        document.update(modules=sorted(modules), api_version="pg_maker/contrib-inventory/v1")
        write_json(branch.output / "contrib-files.json", document)
        branch.report["standard_contrib"] = {
            "modules": sorted(modules),
            "files": sum(e["type"] != "directory" for e in document["entries"]),
        }
    finally:
        shutil.rmtree(staging)


def selected_files(branch, files, omit):
    """Fail closed if a module vanished or an external install changed its bytes."""
    if not enabled(branch.config):
        return set()
    path = branch.output / "contrib-files.json"
    if not path.is_file():
        raise PreconditionError("standard contrib inventory is missing; rebuild the core")
    document = read_json(path)
    if not document.get("modules") or not document.get("entries"):
        raise PreconditionError("standard contrib inventory is empty")
    selected = set()
    for entry in document["entries"]:
        if entry["type"] == "directory" or omit(entry["path"]):
            continue
        if files.get(entry["path"]) != entry:
            raise PreconditionError(
                "standard contrib file is missing or changed", details={"path": entry["path"]}
            )
        selected.add(entry["path"])
    return selected
