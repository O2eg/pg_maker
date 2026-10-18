"""Cache each component's added install files independently of its neighbours."""

import os
import shutil
import tempfile
from pathlib import Path

from pg_maker.builders.components import build_components
from pg_maker.errors import MakerError, PreconditionError
from pg_maker.execution.cache import SnapshotStore
from pg_maker.execution.incremental import previous, seed_component
from pg_maker.execution.resources import check_resources
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, read_json, write_json


def entries(root):
    return {e["path"]: e for e in inventory(root)["entries"]}


def merge_install(source, target):
    incoming, existing = entries(source), entries(target)
    for name, item in incoming.items():
        destination = target / name
        if any(p.is_symlink() for p in destination.parents):
            raise PreconditionError("component install traverses a symlink")
        if name in existing and item != existing[name]:
            if item["type"] == existing[name]["type"] == "directory":
                continue
            raise PreconditionError(
                "component install conflicts with existing files", details={"path": name}
            )
    for name, item in incoming.items():
        destination = target / name
        if name in existing:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if item["type"] == "directory":
            destination.mkdir(exist_ok=True)
        elif item["type"] == "symlink":
            destination.symlink_to(os.readlink(source / name))
        else:
            shutil.copy2(source / name, destination)


def execute_component(branch, component, *, run_tests):
    identifier = component["metadata"]["id"]
    store = SnapshotStore(Path(branch.request.cache_store)) if branch.request.cache_store else None
    key = content_hash(
        {
            "adapter": "pg_maker/component-install/v2",
            "stage": branch.step["stage_key"],
            "branch": branch.id,
        }
    )
    prefix = branch.root / "prefix"
    workspace = branch.root / "components" / identifier
    if store and not run_tests:
        store.pin(branch.context.directory, key)
        try:
            cached = store.lookup(key)
            source_store = store
            if not cached:
                with previous(branch) as prior:
                    if prior and prior[1].cache_store:
                        source_store = SnapshotStore(Path(prior[1].cache_store))
                        cached = source_store.lookup(key)
            if cached:
                snapshot = source_store.verify(cached["snapshot_hash"])
                check_resources(
                    branch.context.directory,
                    branch.config["defaults"]["execution"]["limits"],
                    extra_bytes=sum(e.get("size", 0) for e in snapshot["entries"]),
                )
                with tempfile.TemporaryDirectory(prefix="component-cache-", dir=branch.root) as tmp:
                    destination = Path(tmp)
                    roots = {
                        name: destination / name for name in ("install", "evidence", "workspace")
                    }
                    source_store.restore(
                        cached["snapshot_hash"],
                        roots,
                        check=branch.context.check,
                    )
                    if source_store.directory != store.directory:
                        store.capture(
                            roots,
                            {"kind": "component"},
                            key=key,
                            producer=cached["producer"],
                            operation=branch.context.directory,
                            check=branch.context.check,
                        )
                    merge_install(destination / "install", prefix)
                    report = read_json(destination / "evidence/component.json")
                    report["cache_producer"] = cached["producer"]
                    evidence = branch.output / "component-cache" / identifier
                    shutil.copytree(destination / "evidence", evidence)
                    if (destination / "workspace").exists():
                        workspace.parent.mkdir(exist_ok=True)
                        os.replace(destination / "workspace", workspace)
                branch.report.setdefault("components", []).append(report)
                branch.context.event("component_reused", step_id=branch.step["id"])
                return
        except (MakerError, OSError, ValueError, KeyError) as exc:
            branch.context.check()
            branch.context.progress.info("Component cache unavailable; rebuilding: " + str(exc))
    before = entries(prefix)
    first_command = branch.counter + 1
    seed_component(branch, component)
    branch.prepare()
    build_components(branch, [component], run_tests=run_tests)
    after = entries(prefix)
    removed = before.keys() - after.keys()
    overwritten = [
        name
        for name in before.keys() & after.keys()
        if before[name] != after[name] and before[name]["type"] != "directory"
    ]
    if removed or overwritten:
        raise PreconditionError(
            "component modified files owned by the core or another component",
            details={"removed": sorted(removed), "overwritten": overwritten},
        )
    if store and not run_tests:
        with tempfile.TemporaryDirectory(prefix="component-output-", dir=branch.root) as tmp:
            root = Path(tmp)
            install, evidence = root / "install", root / "evidence"
            install.mkdir()
            evidence.mkdir()
            for name in sorted(after.keys() - before.keys()):
                item, source, destination = after[name], prefix / name, install / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if item["type"] == "directory":
                    destination.mkdir(exist_ok=True)
                elif item["type"] == "symlink":
                    destination.symlink_to(os.readlink(source))
                else:
                    shutil.copy2(source, destination)
            write_json(evidence / "component.json", branch.report["components"][-1])
            for number in range(first_command, branch.counter + 1):
                record = branch.output / "commands" / f"{number}.json"
                if not record.exists():
                    continue
                (evidence / "commands").mkdir(exist_ok=True)
                shutil.copy2(record, evidence / "commands" / record.name)
                for suffix in ("stdout.log", "stderr.log"):
                    log = branch.output / "command-logs" / f"{number}.{suffix}"
                    if log.exists():
                        (evidence / "command-logs").mkdir(exist_ok=True)
                        shutil.copy2(log, evidence / "command-logs" / log.name)
            producer = {
                "run_id": branch.context.directory.name,
                "lock_hash": branch.request.lock["lock_hash"],
                "component": identifier,
            }
            store.capture(
                {"install": install, "evidence": evidence, "workspace": workspace},
                {"kind": "component"},
                key=key,
                producer=producer,
                operation=branch.context.directory,
                check=branch.context.check,
            )
