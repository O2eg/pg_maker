"""Reviewed removal of private scratch after a verified durable export."""

import os
import shutil
from pathlib import Path

from pg_maker.errors import PreconditionError
from pg_maker.models import TERMINAL_STATUSES
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, exclusive_lock, read_json, write_json


def _export(directory: Path, exported: Path) -> dict:
    if exported.is_relative_to(directory) or any(
        p.is_symlink() for p in (exported, *exported.parents)
    ):
        raise PreconditionError("cleanup requires an external non-symlink export")
    manifest = read_json(exported / "export-manifest.json")
    actual = inventory(exported)
    actual["entries"] = [e for e in actual["entries"] if e["path"] != "export-manifest.json"]
    if (
        manifest.get("operation_id") != directory.name
        or actual != manifest["inventory"]
        or content_hash(actual) != manifest["tree_hash"]
    ):
        raise PreconditionError("export inventory failed verification")
    # Export must cover this exact operation/attempt, not a previous failed attempt.
    if read_json(exported / "state.json") != read_json(directory / "state.json"):
        raise PreconditionError("operation changed since export")
    if read_json(exported / "input.json") != read_json(directory / "input.json"):
        raise PreconditionError("operation input differs from export")
    if (directory / "workspace/output").exists() and inventory(
        directory / "workspace/output"
    ) != inventory(exported / "output"):
        raise PreconditionError("operation outputs differ from export")
    return manifest


def cleanup_plan(service, directory: Path, exported: Path) -> dict:
    directory = service._reference(directory)
    state = service.status(directory)
    if state["status"] not in TERMINAL_STATUSES or state["resources"]:
        raise PreconditionError("cleanup requires a terminal operation with no owned resources")
    manifest = _export(directory, exported)
    entries = []
    for name in ("workspace/build", "workspace/source", "checkpoints"):
        path = directory / name
        if not path.exists():
            continue
        if (
            any(p.is_symlink() for p in (path, *path.parents))
            or path.stat().st_dev != directory.stat().st_dev
        ):
            raise PreconditionError("scratch must not be a symlink or another filesystem")
        size = 0
        for root, dirs, files in os.walk(path, followlinks=False):
            for child in [*dirs, *files]:
                item = Path(root) / child
                stat = item.lstat()
                if stat.st_dev != directory.stat().st_dev or item.is_mount():
                    raise PreconditionError("scratch contains a mount")
                size += stat.st_blocks * 512
        entries.append({"path": name, "inode": path.stat().st_ino, "allocated_bytes": size})
    result = {
        "api_version": "pg_maker/cleanup-plan/v1",
        "operation": str(directory),
        "export": str(exported),
        "export_hash": manifest["tree_hash"],
        "state_hash": content_hash(state),
        "entries": entries,
        "resume_available_after_cleanup": False,
    }
    result["plan_hash"] = content_hash(result)
    return result


def apply_cleanup(service, plan: dict) -> dict:
    directory = service._reference(Path(plan["operation"]))
    with exclusive_lock(directory / "owner.lock", blocking=False):
        # status can acquire owner.lock separately only outside this guard.
        state = read_json(directory / "state.json")
        if (
            content_hash(state) != plan["state_hash"]
            or state["resources"]
            or state["status"] not in TERMINAL_STATUSES
        ):
            raise PreconditionError("operation changed after cleanup planning")
        if plan["plan_hash"] != content_hash({k: v for k, v in plan.items() if k != "plan_hash"}):
            raise PreconditionError("cleanup plan hash changed")
        manifest = _export(directory, Path(plan["export"]))
        if manifest["tree_hash"] != plan["export_hash"]:
            raise PreconditionError("export changed after cleanup planning")
        for entry in plan["entries"]:
            if entry["path"] not in {"workspace/build", "workspace/source", "checkpoints"}:
                raise PreconditionError("cleanup path is outside private scratch")
            path = directory / entry["path"]
            if (
                any(p.is_symlink() for p in (path, *path.parents))
                or path.stat().st_ino != entry["inode"]
                or path.stat().st_dev != directory.stat().st_dev
                or path.is_mount()
            ):
                raise PreconditionError("scratch identity changed")
            # Refuse mounts inserted after preview as well.
            for root, dirs, files in os.walk(path, followlinks=False):
                for child in [*dirs, *files]:
                    item = Path(root) / child
                    if item.lstat().st_dev != directory.stat().st_dev or item.is_mount():
                        raise PreconditionError("scratch contains a mount")
        before = shutil.disk_usage(directory).free
        write_json(
            directory / "scratch-cleaned.json",
            {"plan_hash": plan["plan_hash"], "export": plan["export"], "resume_available": False},
        )
        for entry in plan["entries"]:
            shutil.rmtree(directory / entry["path"])
        return {
            "removed": [e["path"] for e in plan["entries"]],
            "export": plan["export"],
            "free_bytes_before": before,
            "free_bytes_after": shutil.disk_usage(directory).free,
        }
