"""Recovery boundaries for one branch; no shared mutable compilation directory."""

import os
import uuid
from pathlib import Path

from pg_maker.errors import MakerError
from pg_maker.execution.cache import SnapshotStore
from pg_maker.execution.incremental import previous
from pg_maker.execution.resources import check_resources
from pg_maker.storage import content_hash, ensure_directory, read_json, write_json

BOUNDARIES = {
    "integrate_source_components",
    "configure",
    "compile_core",
    "install_build_prefix",
    "compile_components",
    "install_components",
    "build_tools",
    "collect_install_tree",
    "runtime_dependencies",
    "package",
    "prepare_documentation",
    "export_documentation",
}


class Checkpoints:
    def __init__(self, branch, steps):
        self.branch, self.steps = branch, steps
        operation = branch.context.directory
        self.store = SnapshotStore(operation / "checkpoints/store")
        ensure_directory(self.store.directory)
        write_json(self.store.directory / "checkpoint-store", {"operation": operation.name})
        self.index = operation / "checkpoints" / branch.id
        self.shared = (
            SnapshotStore(Path(branch.request.cache_store)) if branch.request.cache_store else None
        )
        self.build_end = max(
            (i for i, s in enumerate(steps) if s["kind"] == "install_components"),
            default=-1,
        )
        self.key = content_hash(
            {
                "adapter": "pg_maker/core-prefix/v2",
                "stage": steps[self.build_end]["stage_key"] if self.build_end >= 0 else None,
                "branch": branch.id,
            }
        )
        self.first_test = next(
            (
                i
                for i, s in enumerate(steps)
                if s["cache"] == "never" and s["kind"] != "verify_inputs"
            ),
            len(steps),
        )

    def _archive(self):
        branch = self.branch
        for label, path in (("build", branch.root), ("output", branch.output)):
            if path.exists():
                destination = (
                    branch.context.attempt_directory
                    / "previous"
                    / branch.id
                    / (label + "-" + uuid.uuid4().hex)
                )
                ensure_directory(destination.parent)
                os.replace(path, destination)

    def restore(self) -> int:
        branch = self.branch
        self._archive()
        destinations = {
            "root": branch.root,
            "source": branch.source,
            "prefix": branch.root / "prefix",
            "output": branch.output,
        }
        records = []
        for path in self.index.glob("*.json"):
            try:
                record = read_json(path)
                if record["plan_hash"] == branch.request.plan["plan_hash"]:
                    records.append(record)
            except (MakerError, OSError, KeyError, ValueError):
                continue
        records.sort(key=lambda r: (r["complete"], r["index"], r["attempt"]), reverse=True)
        for record in records:
            i = record["index"]
            if not 0 <= i < len(self.steps) or record["stage_key"] != self.steps[i]["stage_key"]:
                continue
            if (
                branch.request.plan["spec"]["run_db_tests"]
                and not record["complete"]
                and i >= self.first_test
            ):
                continue
            try:
                self.store.restore(
                    record["snapshot_hash"], destinations, check=branch.context.check
                )
                branch.context.event("checkpoint_restored", step_id=self.steps[i]["id"])
                return i
            except (MakerError, OSError, KeyError, ValueError) as exc:
                if branch.context.token.requested():
                    raise
                self._archive()
                branch.context.progress.info("Checkpoint invalid; rebuilding: " + str(exc))
        if self.shared and not branch.request.plan["spec"]["run_db_tests"] and self.build_end >= 0:
            try:
                self.shared.pin(branch.context.directory, self.key)
                cached = self.shared.lookup(self.key)
                if cached:
                    self.shared.restore(
                        cached["snapshot_hash"], destinations, check=branch.context.check
                    )
                    branch.report["cache_producer"] = cached["producer"]
                    branch.context.event("cache_reused", step_id=self.steps[self.build_end]["id"])
                    return self.build_end
            except (MakerError, OSError, KeyError, ValueError) as exc:
                if branch.context.token.requested():
                    raise
                self._archive()
                branch.context.progress.info("Cache invalid; rebuilding: " + str(exc))
        if not branch.request.plan["spec"]["run_db_tests"] and self.build_end >= 0:
            with previous(branch) as prior:
                if prior:
                    root, request, _, _ = prior
                    operation = root.parents[2]
                    store = SnapshotStore(operation / "checkpoints/store")
                    for path in sorted(
                        (operation / "checkpoints" / branch.id).glob("*.json"), reverse=True
                    ):
                        try:
                            record = read_json(path)
                            if record["stage_key"] != self.steps[self.build_end]["stage_key"]:
                                continue
                            document = store.verify(record["snapshot_hash"])
                            check_resources(
                                branch.context.directory,
                                branch.config["defaults"]["execution"]["limits"],
                                extra_bytes=sum(e.get("size", 0) for e in document["entries"]),
                            )
                            store.restore(
                                record["snapshot_hash"], destinations, check=branch.context.check
                            )
                            branch.report["cache_producer"] = {
                                "run_id": operation.name,
                                "lock_hash": request.lock["lock_hash"],
                                "plan_hash": request.plan["plan_hash"],
                            }
                            if self.shared:
                                self.shared.capture(
                                    {"root": branch.root, "output": branch.output},
                                    {"kind": "core-prefix"},
                                    key=self.key,
                                    producer=branch.report["cache_producer"],
                                    operation=branch.context.directory,
                                    check=branch.context.check,
                                )
                            branch.context.event(
                                "prior_checkpoint_reused", step_id=self.steps[self.build_end]["id"]
                            )
                            return self.build_end
                        except (MakerError, OSError, KeyError, ValueError):
                            branch.context.check()
                            self._archive()
        return -1

    def save(self, index, *, complete=False):
        branch, step = self.branch, self.steps[index]
        if not complete and (
            (
                step["kind"] not in BOUNDARIES
                and not step["kind"].startswith(("translate_docs_", "build_docs_"))
            )
            or (branch.request.plan["spec"]["run_db_tests"] and index >= self.first_test)
        ):
            return
        roots = (
            {"source": branch.source, "prefix": branch.root / "prefix", "output": branch.output}
            if complete
            else {"root": branch.root, "output": branch.output}
        )
        check_resources(branch.context.directory, branch.config["defaults"]["execution"]["limits"])
        metadata = {
            "step_id": step["id"],
            "stage_key": step["stage_key"],
            "plan_hash": branch.request.plan["plan_hash"],
            "complete": complete,
            "index": index,
            "attempt": branch.context.state["attempt"],
        }
        snapshot = self.store.capture(roots, metadata, check=branch.context.check)
        record = {**metadata, "snapshot_hash": snapshot["snapshot_hash"]}
        write_json(self.index / f"{metadata['attempt']:06d}-{index:06d}.json", record)
        branch.context.event("checkpoint_saved", step_id=step["id"])
        if (
            self.shared
            and not branch.request.plan["spec"]["run_db_tests"]
            and index == self.build_end
        ):
            producer = {
                "run_id": branch.context.directory.name,
                "lock_hash": branch.request.lock["lock_hash"],
                "plan_hash": branch.request.plan["plan_hash"],
            }
            self.shared.capture(
                {"root": branch.root, "output": branch.output},
                {"kind": "core-prefix", "producer": producer},
                check=branch.context.check,
                key=self.key,
                producer=producer,
                operation=branch.context.directory,
            )
