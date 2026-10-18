"""Development sessions connect private Git trees to ordinary immutable build runs."""

from __future__ import annotations

import copy
import os
import shutil
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path

from pg_maker.changes.apply import apply_patch, insert_text
from pg_maker.development.git import PatchStack, git
from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.models import TERMINAL_STATUSES
from pg_maker.planning.documents import implementation_identity, seal, store_from_document, verify
from pg_maker.planning.plan import build_plan
from pg_maker.planning.reuse import compilation_inputs
from pg_maker.sources.resolver import verify_postgresql_version
from pg_maker.sources.store import InputStore
from pg_maker.storage import content_hash, exclusive_lock, file_hash, read_json, utc_now, write_json
from pg_maker.yamlio import load_yaml


def snapshot(stack: PatchStack, store: InputStore, reserve_bytes: int = 0) -> str:
    """Include tracked and nonignored new files, never Git metadata or build detritus."""
    head = stack.head()
    names = set(
        git(stack.path, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        .stdout.decode()
        .split("\0")
    ) - {""}
    size = sum(
        (stack.path / name).stat().st_size
        for name in names
        if (stack.path / name).is_file() and not (stack.path / name).is_symlink()
    )
    for location in (stack.root, store.root):
        if shutil.disk_usage(location).free < reserve_bytes + 2 * size:
            raise PreconditionError("insufficient disk space for a development source snapshot")
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=stack.root) as temporary:
        root = Path(temporary)
        checks = []
        for name in sorted(names):
            if not relative_output(name) or name == ".git" or name.startswith(".git/"):
                raise ValidationError("unsafe Git source path")
            source, target = stack.path / name, root / name
            if any(p.is_symlink() for p in source.parents):
                raise PreconditionError("Git source path traverses a symlink")
            if not source.exists() and not source.is_symlink():
                continue
            if source.is_dir():
                raise PreconditionError("Git submodules must be materialized as source inputs")
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                link = os.readlink(source)
                target.symlink_to(link)
                checks.append((source, "link", link))
            else:
                digest = file_hash(source)
                shutil.copy2(source, target)
                if file_hash(target) != digest:
                    raise PreconditionError("source changed while taking development snapshot")
                checks.append((source, "file", digest))
        digest = store.put_tree(root)
        for source, kind, expected in checks:
            actual = os.readlink(source) if kind == "link" else file_hash(source)
            if actual != expected:
                raise PreconditionError("source changed while taking development snapshot")
    if stack.head() != head:
        raise PreconditionError("Git HEAD changed while taking development snapshot")
    return digest


def source_reference(digest):
    return {
        "kind": "snapshot",
        "source_hash": digest,
        "selection": {"algorithm": "fnmatchcase-posix/v1", "include": ["**"], "exclude": []},
    }


class DevelopmentSession:
    def __init__(self, directory: Path):
        self.root = directory.absolute()
        if any(p.is_symlink() for p in (self.root, *self.root.parents)):
            raise PreconditionError("development session must not traverse symbolic links")
        self.file = self.root / "session.json"

    def load(self):
        state = read_json(self.file)
        if state.get("format") != "pg_maker/development/v1":
            raise ValidationError("unsupported development session")
        if self.root.stat().st_uid != os.getuid():
            raise PreconditionError("development session belongs to another user")
        return state

    def stack(self, state, scope):
        if scope not in state["trees"]:
            raise ValidationError("unknown development scope", details={"scope": scope})
        name = state["trees"][scope]["directory"]
        if not relative_output(name) or "/" in name:
            raise ValidationError("invalid development tree directory")
        return PatchStack(self.root / "trees" / name)

    @staticmethod
    def _feature(lock, identifier=None):
        features = [
            f
            for f in lock["spec"]["config"]["features"]
            if identifier is None or f["metadata"]["id"] == identifier
        ]
        if len(features) != 1:
            raise ValidationError("select exactly one feature for a development session")
        return features[0]

    def open(
        self,
        *,
        lock: Path | None = None,
        from_run: Path | None = None,
        feature: str | None = None,
        input_store: Path | None = None,
        repository: Path | None = None,
    ):
        from pg_maker.api import MakerService

        if bool(lock) == bool(from_run):
            raise ValidationError("provide exactly one lock or previous run")
        if from_run:
            from_run = MakerService._reference(from_run.absolute())
            state = MakerService(from_run.parent).status(from_run)
            if state["status"] not in TERMINAL_STATUSES:
                raise PreconditionError("wait for the source run before opening a session")
            document = read_json(from_run / "input.json")
            if (
                content_hash(document) != state["input_hash"]
                or document["operation_kind"] != "build"
            ):
                raise PreconditionError("source run has no intact build input")
            locked = document["lock"]
            input_store = input_store or Path(document["input_store"])
        else:
            locked = read_json(lock)
        verify(locked, "lock")
        selected = self._feature(locked, feature)
        store = store_from_document(locked, input_store)
        if self.root.is_relative_to(store.root):
            raise ValidationError("development session must be outside the immutable InputStore")
        config = locked["spec"]["config"]
        sources = {"postgresql": config["source"]["source_hash"]}
        for component in selected["spec"]["components"]:
            if "source" in component["spec"]:
                sources["component:" + component["metadata"]["id"]] = component["spec"]["source"][
                    "source_hash"
                ]
        total = sum(
            sum(
                p.stat().st_size
                for p in store.tree(digest).rglob("*")
                if p.is_file() and not p.is_symlink()
            )
            for digest in sources.values()
        )
        self.root.parent.mkdir(parents=True, exist_ok=True)
        reserve = config["defaults"]["execution"]["limits"]["min_free_disk_bytes"]
        if shutil.disk_usage(self.root.parent).free < reserve + 3 * total:
            raise PreconditionError("insufficient disk space for development trees")
        self.root.mkdir(mode=0o700, exist_ok=False)
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = {
                "format": "pg_maker/development/v1",
                "id": uuid.uuid4().hex,
                "feature": selected["metadata"]["id"],
                "input_store": str(store.root),
                "origin_run": str(from_run) if from_run else None,
                "trees": {},
                "iterations": [],
                "pins": [],
                "created_at": utc_now(),
            }
            write_json(self.root / "base-lock.json", locked)
            for number, (scope, digest) in enumerate(sources.items()):
                name = f"{number:03d}-" + scope.replace(":", "-")
                stack = PatchStack(self.root / "trees" / name)
                stack.initialize(store.tree(digest), repository if scope == "postgresql" else None)
                if scope == "postgresql":
                    if config["defaults"]["common_changes"]:
                        patch = store.blob(locked["spec"]["blobs"]["builtin/unix-socket-directory"])
                        apply_patch(stack.path, patch.read_text())
                        git(stack.path, "add", "-A")
                        git(stack.path, "commit", "--allow-empty", "-m", "Distribution defaults")
                        stack_state = stack.load()
                        stack_state["base"] = stack.head()
                        write_json(stack.file, stack_state)
                    for change in selected["spec"]["changes"]:
                        for operation in change["spec"]["operations"]:
                            blob = store.blob(operation["file"]["sha256"])
                            if operation["kind"] == "patch":
                                apply_patch(stack.path, blob.read_text())
                            else:
                                insert_text(stack.path, load_yaml(blob, "insertions"))
                        stack.commit(change["metadata"]["id"])
                state["trees"][scope] = {
                    "directory": name,
                    "initial_hash": snapshot(stack, store, reserve),
                    "initial_head": stack.head(),
                    "source_hash": digest,
                }
            write_json(self.file, state)
        return self.status()

    def _runs(self, state):
        from pg_maker.api import MakerService

        result = []
        service = MakerService(self.root / "operations")
        for iteration in state["iterations"]:
            item = copy.deepcopy(iteration)
            if not relative_output(item["run_id"]) or "/" in item["run_id"]:
                raise ValidationError("invalid development run reference")
            run = self.root / "operations" / item["run_id"]
            item["status"] = service.status(run)["status"] if run.exists() else "not-started"
            item["pinned"] = item["id"] in state["pins"]
            result.append(item)
        return result

    def status(self):
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            trees = {}
            for scope in state["trees"]:
                stack = self.stack(state, scope)
                trees[scope] = {
                    "source": str(stack.path),
                    "head": stack.head(),
                    "dirty": stack.dirty(),
                    **stack.load(),
                }
            return {
                "directory": str(self.root),
                "id": state["id"],
                "feature": state["feature"],
                "trees": trees,
                "iterations": self._runs(state),
                "disk_free_bytes": shutil.disk_usage(self.root).free,
            }

    def patch(
        self,
        action,
        *,
        scope="postgresql",
        identifier=None,
        patch=None,
        count=1,
        message=None,
        onto_source=None,
        onto_lock=None,
        output=None,
    ):
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            stack = self.stack(state, scope)
            if action == "import":
                if patch is None:
                    raise ValidationError("patch import requires a file or series.json")
                if Path(patch).name == "series.json":
                    series = read_json(Path(patch))
                    if series.get("format") != "pg_maker/patch-series/v1":
                        raise ValidationError("unsupported patch series")
                    patches = []
                    for entry in series["patches"]:
                        if not relative_output(entry["file"]):
                            raise ValidationError("unsafe series patch path")
                        patches.append((Path(patch).parent / entry["file"], entry["id"]))
                    result = stack.import_series(patches)
                else:
                    result = stack.import_patch(Path(patch), identifier)
            elif action == "commit":
                result = stack.commit(identifier, message)
            elif action == "refresh":
                result = stack.refresh(message)
            elif action in {"pop", "push"}:
                result = stack.move(action, count)
            elif action == "adopt":
                result = stack.adopt()
            elif action in {"continue", "abort"}:
                result = stack.continue_operation() if action == "continue" else stack.abort()
                pending = self.root / "pending-base-lock.json"
                if scope == "postgresql" and pending.exists() and not result["pending"]:
                    if action == "continue":
                        os.replace(pending, self.root / "base-lock.json")
                        state["trees"][scope]["rebased"] = True
                    else:
                        pending.unlink()
            elif action == "rebase":
                stack._guard(stack.load())
                stack.clean()
                store = InputStore(Path(state["input_store"]))
                if scope == "postgresql":
                    if onto_lock is None:
                        raise ValidationError("PostgreSQL rebase requires --onto-lock")
                    locked = read_json(Path(onto_lock))
                    verify(locked, "lock")
                    new_feature = self._feature(locked, state["feature"])
                    new_scopes = {
                        "component:" + c["metadata"]["id"]
                        for c in new_feature["spec"]["components"]
                        if "source" in c["spec"]
                    } | {"postgresql"}
                    if new_scopes != state["trees"].keys():
                        raise ValidationError("rebase requires the same component repository set")
                    other_store = store_from_document(locked)
                    # Copy the new lock's complete inputs before changing the workspace.
                    digests = [locked["spec"]["config"]["source"]["source_hash"]]
                    for feature in locked["spec"]["config"]["features"]:
                        digests += [
                            c["spec"]["source"]["source_hash"]
                            for c in feature["spec"]["components"]
                            if "source" in c["spec"]
                        ]
                    for digest in digests:
                        store.put_tree(other_store.tree(digest))
                    for digest in locked["spec"]["blobs"].values():
                        store.put_blob(other_store.blob(digest), digest)
                    onto_source = locked["spec"]["config"]["source"]["source_hash"]
                    with tempfile.TemporaryDirectory(prefix="base-", dir=self.root) as tmp:
                        source = Path(tmp) / "source"
                        shutil.copytree(store.tree(onto_source), source, symlinks=True)
                        if locked["spec"]["config"]["defaults"]["common_changes"]:
                            apply_patch(
                                source,
                                store.blob(
                                    locked["spec"]["blobs"]["builtin/unix-socket-directory"]
                                ).read_text(),
                            )
                        write_json(self.root / "pending-base-lock.json", locked)
                        result = stack.rebase(source)
                    if not result["pending"]:
                        os.replace(
                            self.root / "pending-base-lock.json", self.root / "base-lock.json"
                        )
                        state["trees"][scope]["rebased"] = True
                else:
                    if onto_source is None:
                        raise ValidationError("component rebase requires --onto-source")
                    result = stack.rebase(store.tree(onto_source))
            elif action == "export":
                if output is None:
                    raise ValidationError("patch export requires an output directory")
                result = stack.export(Path(output))
            else:
                raise ValidationError("unknown patch action")
            write_json(self.file, state)
            return {"scope": scope, "source": str(stack.path), "stack": result}

    def _freeze(self, state):
        if (self.root / "pending-base-lock.json").exists():
            raise PreconditionError("complete or abort the PostgreSQL base transition first")
        locked = read_json(self.root / "base-lock.json")
        spec = copy.deepcopy(locked["spec"])
        config = spec["config"]
        feature = self._feature(locked, state["feature"])
        config["features"] = [copy.deepcopy(feature)]
        feature = config["features"][0]
        config["variants"] = [v for v in config["variants"] if v["feature"] == state["feature"]]
        store = InputStore(Path(state["input_store"]))
        records = {}
        for scope, tree in state["trees"].items():
            stack = self.stack(state, scope)
            stack_state = stack.load()
            stack._guard(stack_state)
            digest = snapshot(
                stack, store, config["defaults"]["execution"]["limits"]["min_free_disk_bytes"]
            )
            records[scope] = {
                "head": stack.head(),
                "source_hash": digest,
                "dirty": stack.dirty(),
                "base": stack_state["base"],
                "series": stack_state["entries"][: stack_state["cursor"]],
            }
            if scope == "postgresql":
                if digest != tree["initial_hash"] or tree.get("rebased"):
                    config["source"] = source_reference(digest)
                    config["defaults"]["common_changes"] = []
                    feature["spec"]["changes"] = []
                verify_postgresql_version(
                    store.tree(config["source"]["source_hash"]), config["postgresql"]["version"]
                )
            else:
                identifier = scope.split(":", 1)[1]
                component = next(
                    (c for c in feature["spec"]["components"] if c["metadata"]["id"] == identifier),
                    None,
                )
                if component is None:
                    raise PreconditionError(
                        "new base lock changed the component set; open a new session"
                    )
                if digest != tree["initial_hash"] or state["trees"]["postgresql"].get("rebased"):
                    component["spec"]["source"] = source_reference(digest)
        spec["implementation"] = implementation_identity()
        spec["development"] = {
            "session_id": state["id"],
            "base_lock": locked["lock_hash"],
            "parent_lock": state["iterations"][-1]["lock_hash"]
            if state["iterations"]
            else locked["lock_hash"],
            "trees": records,
        }
        return seal("lock", spec, {"input_store": state["input_store"]})

    def _previous(self, state):
        for item in reversed(state["iterations"]):
            directory = self.root / "operations" / item["run_id"]
            if (directory / "state.json").is_file() and (directory / "input.json").is_file():
                return directory
        return Path(state["origin_run"]) if state["origin_run"] else None

    def _explain(self, state, locked, plan):
        previous = self._previous(state)
        old_steps, old_lock = {}, None
        if previous and (previous / "input.json").exists():
            document = read_json(previous / "input.json")
            old_steps = {s["id"]: s for s in document["plan"]["spec"]["steps"]}
            old_lock = document["lock"]
        reasons = []
        for step in plan["spec"]["steps"]:
            old = old_steps.get(step["id"])
            same = old and old["stage_key"] == step["stage_key"]
            reason = (
                "unchanged inputs; verified cache/checkpoint required"
                if same
                else "new or changed inputs"
            )
            if step["cache"] == "never":
                reason = "always executed"
            if not same and step["kind"] in {"compile_core", "build_tools"} and old_lock:
                try:
                    old_feature = self._feature(old_lock, state["feature"])
                    feature = self._feature(locked, state["feature"])
                    old_variant = next(
                        v
                        for v in old_lock["spec"]["config"]["variants"]
                        if v["target"] == step["target_id"]
                    )
                    variant = next(
                        v
                        for v in locked["spec"]["config"]["variants"]
                        if v["target"] == step["target_id"]
                    )
                    a, ak = compilation_inputs(old_lock, old_feature, old_variant)
                    b, bk = compilation_inputs(locked, feature, variant)
                    changed = [key for key in b if a.get(key) != b[key]]
                    reason = (
                        "core inputs changed: " + ", ".join(changed)
                        if changed
                        else "component source, recipe or dependency changed"
                    )
                except (KeyError, StopIteration):
                    pass
            reasons.append(
                {
                    "step": step["id"],
                    "kind": step["kind"],
                    "inputs_unchanged": bool(same),
                    "reason": reason,
                }
            )
        return {"previous_run": str(previous) if previous else None, "steps": reasons}

    def explain(self, *, goal="build", components=None):
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            locked = self._freeze(state)
            plan = build_plan(locked, goal=goal, run_db_tests=False, components=components)
            return {
                "lock_hash": locked["lock_hash"],
                "plan": plan,
                **self._explain(state, locked, plan),
            }

    def build(self, *, goal="build", components=None):
        from pg_maker.api import MakerService

        if goal not in {"source", "build", "package", "docs"}:
            raise ValidationError("unsupported development goal")
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            if any(
                item["status"] not in TERMINAL_STATUSES | {"not-started"}
                for item in self._runs(state)
            ):
                raise PreconditionError("a development iteration is still running")
            locked = self._freeze(state)
            plan = build_plan(locked, goal=goal, run_db_tests=False, components=components)
            explanation = self._explain(state, locked, plan)
            identifier = f"{len(state['iterations']) + 1:06d}"
            destination = self.root / "iterations" / identifier
            destination.mkdir(parents=True)
            write_json(destination / "lock.json", locked)
            write_json(destination / "plan.json", plan)
            write_json(destination / "reuse.json", explanation)
            previous = self._previous(state)
            item = {
                "id": identifier,
                "parent": state["iterations"][-1]["id"] if state["iterations"] else None,
                "run_id": "iteration-" + identifier,
                "lock_hash": locked["lock_hash"],
                "plan_hash": plan["plan_hash"],
                "created_at": utc_now(),
            }
            state["iterations"].append(item)
            write_json(self.file, state)
            service = MakerService(self.root / "operations")
            result = service.build(
                destination / "lock.json",
                plan=destination / "plan.json",
                plan_hash=plan["plan_hash"],
                cache_store=self.root / "cache",
                components=components,
                reuse_from=previous,
                operation_id=item["run_id"],
            )
            return {
                **result,
                "iteration": identifier,
                "operation_directory": str(service.directory / item["run_id"]),
                "plan_hash": plan["plan_hash"],
                "reuse": explanation,
            }

    def pin(self, iteration, *, pinned=True):
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            if iteration not in {item["id"] for item in state["iterations"]}:
                raise ValidationError("unknown development iteration")
            selected = next(item for item in state["iterations"] if item["id"] == iteration)
            if (
                pinned
                and (
                    self.root / "operations" / selected["run_id"] / "scratch-cleaned.json"
                ).exists()
            ):
                raise PreconditionError("iteration scratch was already cleaned")
            pins = set(state["pins"])
            pins.add(iteration) if pinned else pins.discard(iteration)
            state["pins"] = sorted(pins)
            write_json(self.file, state)
            return {"iteration": iteration, "pinned": pinned}

    def execute(self, argv, *, iteration=None, component=None, target=None, timeout_seconds=3600):
        from pg_maker.api import MakerService
        from pg_maker.models import DevelopmentCommandRequest

        if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValidationError("debug command requires a nonempty argv")
        if type(timeout_seconds) is not int or timeout_seconds < 1:
            raise ValidationError("debug timeout must be positive")
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            selected = [
                item for item in state["iterations"] if iteration is None or item["id"] == iteration
            ]
            if not selected:
                raise PreconditionError("no development iteration is available")
            run = self.root / "operations" / selected[-1]["run_id"]
            document = read_json(run / "input.json")
            steps = document["plan"]["spec"]["steps"]
            targets = {s["target_id"] for s in steps if target is None or s["target_id"] == target}
            if len(targets) != 1:
                raise ValidationError("select exactly one target for the debug command")
            target = next(iter(targets))
            step = next(
                (s for s in steps if s["target_id"] == target and s["kind"] == "compile_core"), None
            )
            cwd = "obj"
            if component:
                step = next(
                    (
                        s
                        for s in steps
                        if s["target_id"] == target
                        and s["parameters"].get("component_id") == component
                    ),
                    None,
                )
                if step:
                    recipe = step["parameters"]["components"][0]["spec"]
                    cwd = (
                        "obj/" + recipe.get("source_directory", "contrib/" + component)
                        if recipe["type"] in {"contrib", "source-tree"}
                        else "components/"
                        + component
                        + (
                            "/" + recipe["source_directory"]
                            if recipe.get("source_directory")
                            else ""
                        )
                    )
            if step is None:
                raise PreconditionError("selected iteration has no requested build environment")
            request = DevelopmentCommandRequest(
                str(run), content_hash(document), step, tuple(argv), cwd, timeout_seconds
            )
            service = MakerService(self.root / "debug-operations")
            result = service.start_operation(request)
            return {
                **result,
                "operation_directory": str(service.directory / result["operation_id"]),
            }

    def prune(self, *, keep_last=2, apply=None):
        if keep_last < 1:
            raise ValidationError("keep_last must retain the current iteration")
        with exclusive_lock(self.root / "session.lock", blocking=False):
            state = self.load()
            runs = self._runs(state)
            if any(item["status"] not in TERMINAL_STATUSES | {"not-started"} for item in runs):
                raise PreconditionError("cannot prune while a development iteration is running")
            if any(self.stack(state, scope).load()["pending"] for scope in state["trees"]):
                raise PreconditionError("cannot prune during a patch conflict")
            retained = set(state["pins"]) | {item["id"] for item in runs[-keep_last:]}
            files = []
            for item in runs:
                if item["id"] in retained:
                    continue
                for relative in ("workspace/build", "checkpoints"):
                    root = self.root / "operations" / item["run_id"] / relative
                    if any(p.is_symlink() for p in (root, *root.parents)):
                        raise PreconditionError("development scratch must not be a symlink")
                    if root.exists():
                        size = sum(
                            p.lstat().st_size
                            for p in root.rglob("*")
                            if not p.is_symlink() and p.is_file()
                        )
                        files.append(
                            {
                                "iteration": item["id"],
                                "operation": "operations/" + item["run_id"],
                                "path": str(root.relative_to(self.root)),
                                "bytes": size,
                                "fingerprint": scratch_fingerprint(root),
                            }
                        )
            debug_runs = sorted(
                (self.root / "debug-operations").glob("*/state.json"),
                key=lambda p: read_json(p)["created_at"],
            )
            for path in debug_runs[:-keep_last]:
                debug_state = read_json(path)
                if debug_state["status"] not in TERMINAL_STATUSES or debug_state["resources"]:
                    continue
                request = read_json(path.parent / "input.json")
                source_run = Path(request["source_run"]).name
                protected_runs = {i["run_id"] for i in runs if i["id"] in state["pins"]}
                if source_run in protected_runs:
                    continue
                root = path.parent / "workspace/build"
                if any(p.is_symlink() for p in (root, *root.parents)):
                    raise PreconditionError("development scratch must not be a symlink")
                if root.exists():
                    files.append(
                        {
                            "iteration": "debug:" + path.parent.name,
                            "operation": str(path.parent.relative_to(self.root)),
                            "path": str(root.relative_to(self.root)),
                            "bytes": sum(
                                p.lstat().st_size
                                for p in root.rglob("*")
                                if p.is_file() and not p.is_symlink()
                            ),
                            "fingerprint": scratch_fingerprint(root),
                        }
                    )
            core = {
                "session_id": state["id"],
                "keep_last": keep_last,
                "retained": sorted(retained),
                "files": files,
            }
            plan = {
                **core,
                "plan_hash": content_hash(core),
                "bytes": sum(f["bytes"] for f in files),
            }
            if apply is not None:
                if apply != plan:
                    raise PreconditionError("development prune plan changed; preview it again")
                with ExitStack() as owners:
                    owned_runs = {
                        entry["operation"]: self.root / entry["operation"] for entry in files
                    }
                    for run in owned_runs.values():
                        owners.enter_context(exclusive_lock(run / "owner.lock", blocking=False))
                        current = read_json(run / "state.json")
                        if current["status"] not in TERMINAL_STATUSES or current["resources"]:
                            raise PreconditionError("a prune target became active")
                    for entry in files:
                        if scratch_fingerprint(self.root / entry["path"]) != entry["fingerprint"]:
                            raise PreconditionError("development scratch changed after preview")
                    for entry in files:
                        run = owned_runs[entry["operation"]]
                        shutil.rmtree(self.root / entry["path"])
                        write_json(
                            run / "scratch-cleaned.json", {"development_prune": plan["plan_hash"]}
                        )
                    from pg_maker.execution.cache import SnapshotStore

                    cache = SnapshotStore(self.root / "cache")
                    for run in owned_runs.values():
                        cache.unpin(run)
                plan["applied"] = True
            return plan


def scratch_fingerprint(root):
    records = []
    for path in sorted(root.rglob("*")):
        value = path.lstat()
        records.append(
            (
                path.relative_to(root).as_posix(),
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_mode,
            )
        )
    return content_hash(records)
