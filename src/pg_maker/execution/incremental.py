"""Private object reuse is an accelerator; configure/make still determine success."""

import os
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.execution.resources import check_resources
from pg_maker.models import TERMINAL_STATUSES, parse_request
from pg_maker.planning.reuse import compilation_inputs, compilation_recipe
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, exclusive_lock, file_hash, read_json


@contextmanager
def previous(branch):
    reference = branch.request.reuse_from
    if not reference:
        yield None
        return
    directory = Path(reference)
    if any(p.is_symlink() for p in (directory, *directory.parents)):
        raise PreconditionError("reuse source must not be a symlink")
    with exclusive_lock(directory / "owner.lock", blocking=False):
        state = read_json(directory / "state.json")
        document = read_json(directory / "input.json")
        if state["status"] not in TERMINAL_STATUSES or state["resources"]:
            raise PreconditionError("previous iteration is still active or owns resources")
        if content_hash(document) != state["input_hash"]:
            raise PreconditionError("previous iteration input changed")
        request = parse_request(document)
        if request.operation_kind != "build":
            raise PreconditionError("object reuse requires a previous build")
        config = request.lock["spec"]["config"]
        variant = next((v for v in config["variants"] if v["id"] == branch.variant["id"]), None)
        feature = next(
            (
                f
                for f in config["features"]
                if f["metadata"]["id"] == branch.feature["metadata"]["id"]
            ),
            None,
        )
        if variant is None or feature is None:
            yield None
        else:
            root = directory / "workspace/build" / branch.id
            if root.is_symlink() or not root.is_dir():
                yield None
            else:
                yield root, request, feature, variant


def sync_sources(destination: Path, source: Path, old_entries=None):
    """Preserve unchanged mtimes; remove retired inputs without following links."""
    old = {e["path"]: e for e in (old_entries or inventory(destination)["entries"])}
    new = {e["path"]: e for e in inventory(source)["entries"]}
    for name in sorted(old.keys() - new.keys(), key=lambda s: (s.count("/"), s), reverse=True):
        path = destination / name
        if any(p.is_symlink() for p in path.parents):
            raise PreconditionError("incremental source traverses a symlink")
        if path.is_dir() and not path.is_symlink():
            # Generated objects below a retired source directory must not survive it.
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    for name, entry in new.items():
        target, incoming = destination / name, source / name
        if any(p.is_symlink() for p in target.parents):
            raise PreconditionError("incremental source traverses a symlink")
        if entry["type"] == "directory":
            target.mkdir(parents=True, exist_ok=True)
            continue
        unchanged = (
            entry == old.get(name)
            and target.exists()
            and not target.is_symlink()
            and entry["type"] == "file"
            and file_hash(target) == entry["sha256"]
        )
        if unchanged:
            continue
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["type"] == "symlink":
            target.symlink_to(os.readlink(incoming))
        else:
            shutil.copy2(incoming, target)
            # Strictly newer than copied objects, including a previous build in this second.
            stamp = time.time_ns()
            os.utime(target, ns=(stamp, stamp))


def check_copy_space(branch, *paths):
    size = sum(
        entry.stat().st_size
        for path in paths
        for entry in path.rglob("*")
        if not entry.is_symlink() and entry.is_file()
    )
    check_resources(
        branch.context.directory,
        branch.config["defaults"]["execution"]["limits"],
        extra_bytes=size,
    )


def seed_core(branch, source):
    with previous(branch) as prior:
        if prior is None:
            return False
        root, request, feature, variant = prior
        old, _ = compilation_inputs(request.lock, feature, variant, branch.branch)
        new, _ = compilation_inputs(
            branch.request.lock, branch.feature, branch.variant, branch.branch
        )
        for value in (old, new):
            value.pop("source")
            value.pop("changes")
            value.pop("common")
        if old != new or not (root / "obj").is_dir() or not (root / "source").is_dir():
            return False
        check_copy_space(branch, root / "obj", root / "source")
        shutil.copytree(root / "obj", branch.obj, symlinks=True, dirs_exist_ok=True)
        shutil.copytree(root / "source", branch.source, symlinks=True)
        sync_sources(branch.source, source)
        branch.report["incremental_core"] = {"from": str(root), "verified": False}
        return True


def seed_component(branch, component):
    identifier, spec = component["metadata"]["id"], component["spec"]
    if spec["type"] in {"contrib", "source-tree"}:
        return False
    with previous(branch) as prior:
        if prior is None:
            return False
        root, request, feature, variant = prior
        old_core, old_keys = compilation_inputs(request.lock, feature, variant, branch.branch)
        new_core, new_keys = compilation_inputs(
            branch.request.lock, branch.feature, branch.variant, branch.branch
        )
        old_component = next(
            (c for c in feature["spec"]["components"] if c["metadata"]["id"] == identifier), None
        )
        if old_component is None or old_core != new_core:
            return False
        old_recipe, new_recipe = compilation_recipe(old_component), compilation_recipe(component)
        old_recipe.pop("source", None)
        new_recipe.pop("source", None)
        if old_recipe != new_recipe or any(
            old_keys.get(dep) != new_keys[dep] for dep in spec["requires"]
        ):
            return False
        previous_path = root / "components" / identifier
        if previous_path.is_symlink() or not previous_path.is_dir():
            return False
        destination = branch.root / "components" / identifier
        check_copy_space(branch, previous_path)
        shutil.copytree(previous_path, destination, symlinks=True)
        from pg_maker.sources.store import InputStore

        old_source = InputStore(Path(request.input_store)).tree(
            old_component["spec"]["source"]["source_hash"]
        )
        sync_sources(
            destination,
            branch.inputs.tree(spec["source"]["source_hash"]),
            inventory(old_source)["entries"],
        )
        branch.report.setdefault("incremental_components", []).append(identifier)
        return True
