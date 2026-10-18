"""Private agent workspaces and deterministic text-only ChangeSet export."""

import copy
import difflib
import shutil
from pathlib import Path

from pg_maker.documents import relative_output, validate_document
from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.sources.store import InputStore, inventory
from pg_maker.storage import content_hash, file_hash, read_json, write_json, write_text
from pg_maker.yamlio import load_yaml


def workspace(
    store: Path,
    source_hash: str,
    destination: Path,
    *,
    lock: Path | None = None,
    feature: str | None = None,
    until: str | None = None,
) -> dict:
    inputs = InputStore(store)
    source = inputs.tree(source_hash)
    selected, locked = None, None
    if lock is not None:
        from pg_maker.planning.documents import verify

        locked = read_json(lock)
        verify(locked, "lock")
        if locked["spec"]["config"]["source"]["source_hash"] != source_hash:
            raise PreconditionError("workspace source differs from locked upstream")
        candidates = [
            f
            for f in locked["spec"]["config"]["features"]
            if feature is None or f["metadata"]["id"] == feature
        ]
        if len(candidates) != 1:
            raise ValidationError("select exactly one locked feature for the workspace")
        selected = copy.deepcopy(candidates[0])
        if until is not None:
            prefix, found = [], False
            for change in selected["spec"]["changes"]:
                operations = []
                for operation in change["spec"]["operations"]:
                    if change["metadata"]["id"] + "/" + operation["id"] == until:
                        found = True
                        break
                    operations.append(operation)
                if operations:
                    change["spec"]["operations"] = operations
                    if found:
                        change["spec"].pop("postconditions", None)
                    prefix.append(change)
                if found:
                    break
            if not found:
                raise ValidationError("workspace --until must name CHANGE_ID/OPERATION_ID")
            selected["spec"]["changes"] = prefix
    elif feature is not None or until is not None:
        raise ValidationError("workspace feature/until requires a BuildLock")
    if destination.resolve().is_relative_to(store.resolve()):
        raise ValidationError("agent workspace must be outside the immutable InputStore")
    if destination.exists() or destination.is_symlink():
        raise PreconditionError("agent workspace must be a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = sum(p.stat().st_size for p in source.rglob("*") if p.is_file() and not p.is_symlink())
    if shutil.disk_usage(destination.parent).free < size * (3 if locked else 1) + 4 * 1024**3:
        raise PreconditionError("insufficient free disk space for private agent workspace")
    destination.mkdir()
    shutil.copytree(source, destination / "source", symlinks=True)
    if content_hash(inventory(destination / "source")) != source_hash:
        raise PreconditionError("source changed while creating agent workspace")
    upstream = source_hash
    if locked is not None:
        from pg_maker.changes.apply import apply_changes

        apply_changes(
            destination / "source", selected, locked, inputs, "candidate", destination / "receipts"
        )
        source_hash = inputs.put_tree(destination / "source")
    result = {
        "api_version": "pg_maker/change-workspace/v1",
        "input_store": str(store),
        "source_hash": source_hash,
        "upstream_source_hash": upstream,
        "basis": "changes-prefix" if locked else "upstream",
        "lock_hash": locked["lock_hash"] if locked else None,
        "until": until,
        "source": str(destination / "source"),
    }
    write_json(destination / "workspace.json", result)
    return result


def validate_changes(path: Path) -> dict:
    document = load_yaml(path, "change-set")
    references = []
    for operation in document["spec"]["operations"]:
        relative = operation["file"]
        if not relative_output(relative):
            raise ValidationError("ChangeSet operations must refer to relative files")
        target = path.parent / relative
        if not target.is_file() or any(p.is_symlink() for p in (target, *target.parents)):
            raise PreconditionError("ChangeSet operation file is missing or traverses a symlink")
        references.append({"path": str(target), "sha256": file_hash(target)})
        if operation["kind"] == "insert_text":
            load_yaml(target, "insertions")
    acceptance = []
    for test in document["spec"]["acceptance"]:
        if not relative_output(test["script"]):
            raise ValidationError("acceptance script must be relative")
        target = path.parent / test["script"]
        if not target.is_file() or any(p.is_symlink() for p in (target, *target.parents)):
            raise PreconditionError("acceptance script is missing or traverses a symlink")
        if test["kind"] == "command":
            command = load_yaml(target)
            if (
                not isinstance(command, dict)
                or set(command) != {"argv"}
                or not isinstance(command["argv"], list)
                or not command["argv"]
                or not all(isinstance(v, str) and "\x00" not in v for v in command["argv"])
            ):
                raise ValidationError("invalid command acceptance script")
        acceptance.append({"path": str(target), "sha256": file_hash(target)})
    return {
        "valid": True,
        "change_set_hash": content_hash(document),
        "operations": references,
        "acceptance": acceptance,
        "acceptance_executed": False,
    }


def export_changes(directory: Path, output: Path, identifier: str, postgresql: str) -> dict:
    record = read_json(directory / "workspace.json")
    if record.get("api_version") != "pg_maker/change-workspace/v1":
        raise ValidationError("unsupported agent workspace")
    base = InputStore(Path(record["input_store"])).tree(record["source_hash"])
    source = directory / "source"
    if output.resolve().is_relative_to(source.resolve()) or output.resolve().is_relative_to(
        Path(record["input_store"]).resolve()
    ):
        raise ValidationError("ChangeSet export must be outside source and immutable InputStore")
    before = {e["path"]: e for e in inventory(base)["entries"]}
    after = {e["path"]: e for e in inventory(source)["entries"]}
    patches = []
    changed = []
    for name in sorted(before.keys() | after.keys()):
        left, right = before.get(name), after.get(name)
        if left == right:
            continue
        if left and right and left["type"] == right["type"] == "directory" and left != right:
            raise UnsupportedError("directory mode changes cannot be exported")
        if all(e is None or e["type"] == "directory" for e in (left, right)):
            continue
        if left is None and right["mode"] & ~0o666:
            raise UnsupportedError("new text files must have ordinary non-executable permissions")
        if any(e and e["type"] != "file" for e in (left, right)) or (
            left and right and left["mode"] != right["mode"]
        ):
            raise UnsupportedError("ChangeSet export supports text files without mode/link changes")
        try:
            old = (base / name).read_bytes().decode("utf-8") if left else ""
            new = (source / name).read_bytes().decode("utf-8") if right else ""
        except UnicodeError as exc:
            raise UnsupportedError("ChangeSet export requires UTF-8 text files") from exc
        if "\x00" in old + new or (not old and not new):
            raise UnsupportedError("binary and empty-file-only changes need an explicit recipe")
        lines = difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile="a/" + name if left else "/dev/null",
            tofile="b/" + name if right else "/dev/null",
        )
        for line in lines:
            patches.append(
                line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            )
        changed.append(name)
    if not changed:
        raise ValidationError("workspace has no text changes")
    document = {
        "api_version": "pg_maker/change-set/v1",
        "kind": "ChangeSet",
        "metadata": {"id": identifier, "revision": 1},
        "spec": {
            "compatible_postgresql": postgresql,
            "requires": [],
            "conflicts": [],
            "operations": [{"id": "agent-edit", "kind": "patch", "file": "changes.patch"}],
            "acceptance": [],
            "compatibility": {
                "data_format": "unknown",
                "wal_format": "unknown",
                "extension_abi": "unknown",
            },
        },
    }
    validate_document(document, "change-set")
    output.mkdir(parents=True, exist_ok=False)
    write_text(output / "changes.patch", "".join(patches))
    # JSON is a YAML subset and avoids introducing a second serialization convention.
    write_json(output / "change-set.yaml", document)
    write_json(
        output / "workspace-export.json",
        {
            "source_hash": record["source_hash"],
            "changed_files": changed,
            "patch_sha256": file_hash(output / "changes.patch"),
            "acceptance_executed": False,
        },
    )
    return {
        **validate_changes(output / "change-set.yaml"),
        "directory": str(output),
        "changed_files": changed,
        "source_hash": record["source_hash"],
    }
