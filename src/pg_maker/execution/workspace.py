"""One owned mutable workspace, immutable attempt inputs, and small stage receipts."""

import os
import shutil
import uuid

from pg_maker.errors import CommandError, PreconditionError
from pg_maker.execution.incremental import previous, sync_sources
from pg_maker.execution.install import (
    ComponentInstall,
    check_parents,
    payload,
    payload_single,
    same,
)
from pg_maker.planning.reuse import compilation_inputs, compilation_recipe
from pg_maker.storage import content_hash, read_json, sync_directory, write_json

CORE_STAGES = {
    "materialize_source",
    "apply_changes",
    "integrate_source_components",
    "build_libraries",
    "configure",
    "compile_core",
    "install_build_prefix",
    "compile_components",
    "install_components",
}


def archive_evidence(branch, path):
    """Keep failed-attempt test logs while giving the next suite a fresh output."""
    if not path.exists():
        return
    destination = (
        branch.context.attempt_directory
        / "previous-evidence"
        / branch.id
        / uuid.uuid4().hex
        / path.relative_to(branch.output)
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, destination)


class WorkspaceJournal:
    def __init__(self, branch):
        self.branch = branch
        self.path = branch.root / "workspace-journal.json"
        self.reused = False
        self.invalid = set()
        core, self.component_keys = compilation_inputs(
            branch.request.lock, branch.feature, branch.variant, branch.branch
        )
        self.core_key = content_hash(core)
        if not branch.root.exists():
            with previous(branch) as prior:
                if prior and (prior[0] / "workspace-journal.json").exists():
                    root = prior[0]
                    old_output = root.parents[1] / "output" / branch.id
                    branch.root.parent.mkdir(parents=True, exist_ok=True)
                    intent = {
                        "from": str(root),
                        "to": str(branch.root),
                        "operation": str(branch.context.directory),
                    }
                    write_json(
                        branch.context.directory / (branch.id + ".workspace-transfer.json"), intent
                    )
                    # Keep old attempt evidence; build objects keep their inodes and mtimes.
                    if old_output.is_dir():
                        shutil.copytree(
                            old_output,
                            branch.output,
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(
                                "packages", "artifact-manifest.json", "verification.json"
                            ),
                        )
                    os.replace(root, branch.root)
                    sync_directory(root.parent)
                    sync_directory(branch.root.parent)
                    write_json(root.parent / (branch.id + ".transferred.json"), intent)
                    branch.context.event("workspace_transferred", target_id=branch.id)
        self.data = read_json(self.path) if self.path.exists() else {}
        if self.data and (self.data.get("core_key") != self.core_key or not self._valid_core()):
            shutil.rmtree(branch.root)
            (branch.output / "report.json").unlink(missing_ok=True)
            self.data = {}
        self.reused = bool(self.data)
        if self.reused and self.data.get("core_producer"):
            branch.report["cache_producer"] = self.data["core_producer"]
        if not self.data:
            self.data = {
                "format": "pg_maker/workspace/v1",
                "core_key": self.core_key,
                "steps": {},
                "components": {},
                "core_files": {},
            }
        else:
            self._recover_components()
        archive_evidence(branch, branch.output / "package-tests")
        for path in (
            branch.root / "install-tree",
            branch.root / "packaging",
            branch.root / "clusters",
            branch.output / "packages",
        ):
            if path.is_dir():
                shutil.rmtree(path)
        branch.root.mkdir(parents=True, exist_ok=True)
        self.save()

    def _valid_core(self):
        prefix = self.branch.root / "prefix"
        if any(
            identifier.rsplit(".", 1)[-1] in CORE_STAGES
            and (
                record.get("status") in {"running", "interrupted"}
                or (
                    record.get("status") == "failed"
                    and identifier.rsplit(".", 1)[-1] not in {"compile_core", "compile_components"}
                )
            )
            for identifier, record in self.data.get("steps", {}).items()
        ):
            return False
        try:
            return all(
                same(payload_single(prefix / name, name), entry)
                for name, entry in self.data.get("core_files", {}).items()
            )
        except (OSError, ValueError, PreconditionError):
            return False

    def _recover_components(self):
        branch = self.branch
        recipes = {c["metadata"]["id"]: c for c in branch.feature["spec"]["components"]}
        for intent in (branch.root / "install-owners").glob("*.pending.json"):
            ComponentInstall(branch, intent.name.removesuffix(".pending.json")).recover()
        for record in (branch.root / "install-owners").glob("*.json"):
            doc = read_json(record)
            identifier = doc["component"]
            if identifier not in recipes:
                for name in doc["files"]:
                    check_parents(branch.root / "prefix", name).unlink(missing_ok=True)
                record.unlink()
                continue
            try:
                if any(
                    not same(payload_single(branch.root / "prefix" / name, name), entry)
                    for name, entry in doc["files"].items()
                ):
                    self.invalid.add(identifier)
            except (OSError, PreconditionError):
                self.invalid.add(identifier)
        for identifier, component in recipes.items():
            receipt = self.data["components"].get(identifier, {})
            if (
                receipt.get("status") != "succeeded"
                or receipt.get("key") != self.component_keys[identifier]
                or any(dep in self.invalid for dep in component["spec"]["requires"])
            ):
                self.invalid.add(identifier)

    def save(self):
        write_json(self.path, self.data)

    def can_reuse(self, step):
        record = self.data["steps"].get(step["id"], {})
        if record.get("status") != "succeeded" or record.get("key") != step["stage_key"]:
            return False
        if step["cache"] == "never":
            return False
        if step["kind"] in CORE_STAGES:
            # Partial configure/install phases have effects on shared state: rebuild core.
            return True
        if step["kind"] == "build_tools":
            identifier = step["parameters"]["components"][0]["metadata"]["id"]
            for report in self.branch.report.get("components", []):
                if report["id"] == identifier and identifier not in self.invalid:
                    report["cache_producer"] = self.data["components"][identifier].get("producer")
            return identifier not in self.invalid
        return False

    def start(self, step):
        self.data["steps"][step["id"]] = {"key": step["stage_key"], "status": "running"}
        self.save()

    def fail(self, step, error):
        record = self.data["steps"].get(step["id"], {})
        if record.get("status") != "running":
            return
        # A normal compiler/make failure leaves useful partial objects. A killed
        # command, disk failure or lost worker has an ambiguous component tree.
        ordinary = (
            isinstance(error, CommandError)
            and error.details.get("returncode", 0) > 0
            and not error.details.get("timed_out")
        )
        record["status"] = "failed" if ordinary else "interrupted"
        if step["kind"] in {"build_tools", "build_and_test_tools"}:
            identifier = step["parameters"]["components"][0]["metadata"]["id"]
            if identifier in self.data["components"]:
                self.data["components"][identifier]["status"] = record["status"]
        self.save()

    def complete(self, step):
        producer = {
            "run_id": self.branch.context.directory.name,
            "lock_hash": self.branch.request.lock["lock_hash"],
            "plan_hash": self.branch.request.plan["plan_hash"],
        }
        self.data["steps"][step["id"]] = {"key": step["stage_key"], "status": "succeeded"}
        if step["kind"] == "install_components":
            self.data["core_files"] = payload(self.branch.root / "prefix")
            self.data["core_producer"] = producer
        if step["kind"] in {"build_tools", "build_and_test_tools"}:
            identifier = step["parameters"]["components"][0]["metadata"]["id"]
            self.data["components"][identifier]["status"] = "succeeded"
            self.data["components"][identifier]["producer"] = producer
            self.invalid.discard(identifier)
        self.save()

    def prepare_component(self, component):
        branch = self.branch
        identifier, spec = component["metadata"]["id"], component["spec"]
        if spec["type"] in {"contrib", "source-tree"}:
            self.data["components"][identifier] = {"key": self.component_keys[identifier]}
            return
        path = branch.root / "components" / identifier
        receipt = self.data["components"].get(identifier, {})
        recipe = compilation_recipe(component)
        old_recipe = dict(receipt.get("recipe", {}))
        new_recipe = dict(recipe)
        old_source, new_source = old_recipe.pop("source", None), new_recipe.pop("source", None)
        deps = {dep: self.component_keys[dep] for dep in spec["requires"]}
        if path.exists():
            if (
                receipt.get("status") in {"succeeded", "failed"}
                and old_recipe == new_recipe
                and receipt.get("dependencies") == deps
                and old_source
            ):
                if old_source != new_source:
                    from pg_maker.sources.store import inventory

                    sync_sources(
                        path,
                        branch.inputs.tree(new_source),
                        inventory(branch.inputs.tree(old_source))["entries"],
                    )
            else:
                shutil.rmtree(path)
                extra = branch.root / "extra-sources" / identifier
                if extra.exists():
                    shutil.rmtree(extra)
        self.data["components"][identifier] = {
            "key": self.component_keys[identifier],
            "recipe": recipe,
            "dependencies": deps,
            "status": "running",
        }
        self.save()


def compact_success(branch):
    """Keep artifacts/evidence and the small final snapshot, not compiler scratch."""
    execution = branch.config["defaults"]["execution"]
    if execution.get("workspace_retention", "on-failure") == "keep":
        return
    if branch.request.plan["spec"]["goal"] not in {"package", "verified"}:
        return
    if branch.root.exists():
        shutil.rmtree(branch.root)
    validation = branch.context.directory / "validation" / branch.id
    if validation.exists():
        shutil.rmtree(validation)
    for path in branch.context.directory.glob("attempts/*/previous/" + branch.id + "/build-*"):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
    branch.context.event("workspace_compacted", target_id=branch.id)
