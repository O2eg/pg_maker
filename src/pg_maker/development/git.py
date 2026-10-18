"""A small patch stack over a private Git repository; Git owns merge semantics."""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.storage import file_hash, read_json, write_json


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_TERMINAL_PROMPT="0")
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=pg_maker",
        "-c",
        "user.email=builder@localhost",
        "-c",
        "core.autocrlf=false",
        "-C",
        str(path),
        *args,
    ]
    result = subprocess.run(command, env=env, capture_output=True, timeout=120)
    if check and result.returncode:
        raise PreconditionError(
            "Git operation failed",
            details={
                "arguments": list(args),
                "stderr": result.stderr.decode(errors="replace")[-8000:],
            },
        )
    return result


def replace_tree(repository: Path, source: Path) -> None:
    """Replace only an owned checkout; its .git and source store remain untouched."""
    for path in repository.iterdir():
        if path.name == ".git":
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    for path in source.iterdir():
        if path.name == ".git":
            raise PreconditionError("immutable source must not contain .git")
        target = repository / path.name
        if path.is_dir() and not path.is_symlink():
            shutil.copytree(path, target, symlinks=True)
        elif path.is_symlink():
            target.symlink_to(os.readlink(path))
        else:
            shutil.copy2(path, target)


class PatchStack:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "source"
        self.file = root / "stack.json"
        if any(p.is_symlink() for p in (self.path / ".git", self.path, *self.path.parents)):
            raise PreconditionError("development repository must be owned, not linked")

    def initialize(self, source: Path, repository: Path | None = None) -> dict:
        self.root.mkdir(parents=True)
        if repository is None:
            self.path.mkdir()
            git(self.path, "init", "--quiet")
        else:
            repository = repository.resolve(strict=True)
            git(
                self.root,
                "clone",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                "--",
                str(repository),
                str(self.path),
            )
            git(self.path, "checkout", "--detach", "HEAD")
            # No local remotes or credentials are needed for patch development.
            git(self.path, "remote", "remove", "origin")
        replace_tree(self.path, source)
        git(self.path, "add", "-A")
        git(self.path, "commit", "--allow-empty", "-m", "Development source base")
        state = {"base": self.head(), "entries": [], "cursor": 0, "pending": None}
        write_json(self.file, state)
        return state

    def head(self) -> str:
        return git(self.path, "rev-parse", "HEAD").stdout.decode().strip()

    def dirty(self) -> bool:
        return bool(git(self.path, "status", "--porcelain", "--untracked-files=all").stdout)

    def load(self) -> dict:
        if not (self.path / ".git").is_dir():
            raise PreconditionError("development Git repository is missing")
        common = git(self.path, "rev-parse", "--git-common-dir").stdout.decode().strip()
        if (self.path / common).resolve() != (self.path / ".git").resolve():
            raise PreconditionError("Git directory points outside the owned repository")
        top = git(self.path, "rev-parse", "--show-toplevel").stdout.decode().strip()
        if Path(top).resolve() != self.path.resolve():
            raise PreconditionError("Git worktree points outside the owned repository")
        return read_json(self.file)

    def clean(self):
        if self.dirty():
            raise PreconditionError("commit or refresh working changes before moving the stack")

    def _save(self, state):
        write_json(self.file, state)

    def _guard(self, state):
        if state["pending"]:
            raise PreconditionError("resolve the pending patch operation with continue or abort")
        expected = (
            state["entries"][state["cursor"] - 1]["commit"] if state["cursor"] else state["base"]
        )
        if self.head() != expected:
            raise PreconditionError("Git HEAD changed outside the stack; use dev patch adopt")

    def _checkpoint(self):
        git(self.path, "update-ref", "refs/pg-maker/saved/" + uuid.uuid4().hex, self.head())

    def commit(self, identifier: str, message: str | None = None):
        state = self.load()
        self._guard(state)
        if state["cursor"] != len(state["entries"]):
            raise PreconditionError("push the remaining series or refresh the current patch")
        self._identifier(state, identifier)
        git(self.path, "add", "-A")
        git(self.path, "diff", "--cached", "--check")
        git(self.path, "commit", "-m", message or identifier)
        state["entries"].append({"id": identifier, "commit": self.head()})
        state["cursor"] += 1
        self._save(state)
        return state

    @staticmethod
    def _identifier(state, identifier):
        if not identifier or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", identifier):
            raise ValidationError("patch id must be a simple identifier")
        if identifier in {item["id"] for item in state["entries"]}:
            raise ValidationError("patch id already exists")

    def move(self, action: str, count: int = 1):
        state = self.load()
        self._guard(state)
        self.clean()
        cursor = state["cursor"] + (count if action == "push" else -count)
        if count < 1 or not 0 <= cursor <= len(state["entries"]):
            raise ValidationError("patch count exceeds the available series")
        self._checkpoint()
        commit = state["entries"][cursor - 1]["commit"] if cursor else state["base"]
        git(self.path, "checkout", "--detach", commit)
        state["cursor"] = cursor
        self._save(state)
        return state

    def adopt(self):
        state = self.load()
        if state["pending"]:
            raise PreconditionError("cannot adopt commits during a conflict")
        git(self.path, "merge-base", "--is-ancestor", state["base"], "HEAD")
        commits = (
            git(self.path, "rev-list", "--reverse", state["base"] + "..HEAD")
            .stdout.decode()
            .split()
        )
        known = {entry["commit"]: entry for entry in state["entries"]}
        entries = []
        for commit in commits:
            parents = git(self.path, "rev-list", "--parents", "-n", "1", commit).stdout.split()
            if len(parents) != 2:
                raise ValidationError("patch series must be linear; merge commits need flattening")
            entries.append(known.get(commit, {"id": "git-" + commit[:12], "commit": commit}))
        state.update(entries=entries, cursor=len(entries))
        self._save(state)
        return state

    def import_patch(self, patch: Path, identifier: str):
        return self.import_series([(patch, identifier)])

    def import_series(self, patches):
        state = self.load()
        self._guard(state)
        self.clean()
        checked, queue = copy.deepcopy(state), []
        for path, identifier in patches:
            self._identifier(checked, identifier)
            checked["entries"].append({"id": identifier})
            destination = self.root / ("import-" + uuid.uuid4().hex + ".patch")
            shutil.copyfile(path, destination)
            queue.append(
                {"id": identifier, "file": str(destination), "sha256": file_hash(destination)}
            )
        if not queue:
            raise ValidationError("patch series is empty")
        if state["cursor"] != len(state["entries"]):
            raise PreconditionError("push the remaining series before importing")
        self._checkpoint()
        state["pending"] = {
            "kind": "import",
            "before": copy.deepcopy(state),
            "head": self.head(),
            "queue": queue,
        }
        self._save(state)
        return self._import_queue(state)

    def _import_queue(self, state):
        pending = state["pending"]
        while pending["queue"]:
            entry = pending["queue"][0]
            if file_hash(Path(entry["file"])) != entry["sha256"]:
                raise PreconditionError("imported patch bytes changed")
            result = git(self.path, "apply", "--3way", "--index", "--", entry["file"], check=False)
            if result.returncode:
                pending["error"] = result.stderr.decode(errors="replace")[-8000:]
                self._save(state)
                return state
            git(self.path, "diff", "--cached", "--check")
            git(self.path, "commit", "-m", entry["id"])
            state["entries"].append({"id": entry["id"], "commit": self.head()})
            state["cursor"] += 1
            pending["queue"].pop(0)
            self._save(state)
        state["pending"] = None
        self._save(state)
        return state

    def refresh(self, message: str | None = None):
        state = self.load()
        self._guard(state)
        if not state["cursor"]:
            raise PreconditionError("no applied patch to refresh")
        before = copy.deepcopy(state)
        self._checkpoint()
        git(self.path, "add", "-A")
        git(self.path, "diff", "--cached", "--check")
        git(self.path, "commit", "--amend", *(["-m", message] if message else ["--no-edit"]))
        state["entries"][state["cursor"] - 1]["commit"] = self.head()
        state["pending"] = {
            "kind": "replay",
            "before": before,
            "head": before["entries"][before["cursor"] - 1]["commit"],
            "queue": before["entries"][before["cursor"] :],
            "conflict": False,
        }
        state["entries"] = state["entries"][: state["cursor"]]
        self._save(state)
        return self._replay(state)

    def rebase(self, source: Path):
        state = self.load()
        self._guard(state)
        self.clean()
        if state["cursor"] != len(state["entries"]):
            raise PreconditionError("push the entire series before rebasing")
        before, head = copy.deepcopy(state), self.head()
        self._checkpoint()
        git(self.path, "checkout", "--detach", state["base"])
        replace_tree(self.path, source)
        git(self.path, "add", "-A")
        git(self.path, "commit", "--allow-empty", "-m", "Updated development source base")
        state.update(
            base=self.head(),
            entries=[],
            cursor=0,
            pending={
                "kind": "replay",
                "before": before,
                "head": head,
                "queue": before["entries"],
                "conflict": False,
            },
        )
        self._save(state)
        return self._replay(state)

    def _replay(self, state):
        pending = state["pending"]
        while pending["queue"]:
            entry = pending["queue"][0]
            pending["conflict"] = True
            self._save(state)
            result = git(
                self.path, "cherry-pick", "--keep-redundant-commits", entry["commit"], check=False
            )
            if result.returncode:
                return state
            state["entries"].append({**entry, "commit": self.head()})
            state["cursor"] += 1
            pending["queue"].pop(0)
            pending["conflict"] = False
            self._save(state)
        state["pending"] = None
        self._save(state)
        return state

    def continue_operation(self):
        state = self.load()
        pending = state["pending"]
        if not pending:
            raise PreconditionError("no pending patch operation")
        git(self.path, "add", "-A")
        git(self.path, "diff", "--cached", "--check")
        if pending["kind"] == "import":
            entry = pending["queue"][0]
            git(self.path, "commit", "-m", entry["id"])
            state["entries"].append({"id": entry["id"], "commit": self.head()})
            state["cursor"] += 1
            pending["queue"].pop(0)
            self._save(state)
            return self._import_queue(state)
        if pending["conflict"]:
            git(self.path, "-c", "core.editor=true", "cherry-pick", "--continue")
            state["entries"].append({**pending["queue"].pop(0), "commit": self.head()})
            state["cursor"] += 1
            pending["conflict"] = False
            self._save(state)
        return self._replay(state)

    def abort(self):
        state = self.load()
        pending = state["pending"]
        if not pending:
            raise PreconditionError("no pending patch operation")
        git(self.path, "cherry-pick", "--abort", check=False)
        git(self.path, "reset", "--hard", pending["head"])
        self._save(pending["before"])
        return pending["before"]

    def export(self, output: Path):
        state = self.load()
        self._guard(state)
        self.clean()
        output.mkdir(parents=True, exist_ok=False)
        entries = []
        for number, entry in enumerate(state["entries"][: state["cursor"]], 1):
            filename = f"{number:04d}-{entry['id']}.patch"
            patch = git(self.path, "format-patch", "--stdout", "--binary", "-1", entry["commit"])
            (output / filename).write_bytes(patch.stdout)
            entries.append({**entry, "file": filename})
        manifest = {"format": "pg_maker/patch-series/v1", "base": state["base"], "patches": entries}
        write_json(output / "series.json", manifest)
        return manifest
