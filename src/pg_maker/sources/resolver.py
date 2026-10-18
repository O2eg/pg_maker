"""Fetch explicit source inputs, then pin trees and file bytes in InputStore."""

from __future__ import annotations

import re
import tempfile
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from pg_maker.errors import CancelledError, PreconditionError, UnsupportedError, ValidationError
from pg_maker.execution.process import run_process
from pg_maker.sources.store import InputStore, copy_snapshot, extract_archive
from pg_maker.storage import content_hash, ensure_directory


def location(value: str, base: Path) -> str:
    if "\x00" in value or "\n" in value or value.startswith("-"):
        raise ValidationError("invalid source location")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme:
        if parsed.scheme not in {"https", "http", "ssh", "file"}:
            raise UnsupportedError("unsupported source transport")
        if (
            parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.username and parsed.scheme != "ssh")
        ):
            raise ValidationError("source URLs must not embed credentials or query parameters")
        if parsed.scheme == "file":
            if parsed.netloc not in ("", "localhost"):
                raise ValidationError("nonlocal file URL")
            return str(Path(urllib.request.url2pathname(parsed.path)).resolve())
        return value
    if re.match(r"[^/@]+@[^/:]+:", value):
        return value
    return str((base / value).resolve())


class SourceResolver:
    def __init__(
        self,
        store: InputStore,
        directory: Path,
        *,
        offline: bool = False,
        cancelled=lambda: False,
        on_process=None,
    ):
        self.store, self.directory = store, directory
        self.offline, self.cancelled, self.on_process = offline, cancelled, on_process

    def check_cancelled(self):
        if self.cancelled():
            raise CancelledError("input resolution cancelled")

    def _git(self, *args, cwd: Path | None = None, check: bool = True) -> str:
        self.check_cancelled()
        result = run_process(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=cwd,
            log_directory=self.directory / "logs",
            name="git-" + uuid.uuid4().hex,
            env={
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            timeout_seconds=1800,
            cancelled=self.cancelled,
            on_start=self.on_process,
        )
        self.check_cancelled()
        if not result.succeeded and check:
            raise PreconditionError(
                "Git input resolution failed",
                details={
                    "returncode": result.returncode,
                    "stderr": result.stderr_tail,
                    "timed_out": result.timed_out,
                },
            )
        return Path(result.stdout_log).read_text() if result.succeeded else ""

    def _checkout(self, repository: str, ref: str, destination: Path, depth: int = 0) -> dict:
        if depth > 10:
            raise ValidationError("Git submodule nesting exceeds 10")
        if not ref or ref.startswith("-") or any(c in ref for c in "\x00\n\r"):
            raise ValidationError("invalid Git ref")
        with tempfile.TemporaryDirectory(dir=self.directory) as name:
            work = Path(name)
            repo = work / "repo.git"
            self._git("init", "--bare", str(repo))
            self._git("-C", str(repo), "fetch", "--no-tags", "--depth=1", "--", repository, ref)
            commit = self._git(
                "-C", str(repo), "rev-parse", "--verify", "FETCH_HEAD^{commit}"
            ).strip()
            if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                raise PreconditionError("Git did not resolve a commit")
            archive = work / "source.tar"
            # A source snapshot represents the commit tree, not a project's release tarball.
            # info/attributes has precedence over committed export-ignore/export-subst rules.
            (repo / "info/attributes").write_text("* -export-ignore -export-subst\n")
            self._git(
                "-C", str(repo), "archive", "--format=tar", "--output=" + str(archive), commit
            )
            extract_archive(archive, destination)
            submodules = []
            entries = self._git("-C", str(repo), "ls-tree", "-rz", commit).split("\x00")
            gitlinks = [entry.split("\t", 1) for entry in entries if entry.startswith("160000 ")]
            modules_file = destination / ".gitmodules"
            if gitlinks:
                if not modules_file.is_file() or modules_file.is_symlink():
                    raise ValidationError("Git submodules require a regular .gitmodules file")
                paths = self._git(
                    "config", "--file", str(modules_file), "--get-regexp", r"^submodule\..*\.path$"
                )
                mapping = {}
                for line in paths.splitlines():
                    key, path = line.split(" ", 1)
                    mapping[path] = self._git(
                        "config", "--file", str(modules_file), "--get", key[:-4] + "url"
                    ).strip()
                for metadata, path in gitlinks:
                    if path not in mapping:
                        raise ValidationError("missing submodule URL")
                    url = mapping[path]
                    if url.startswith(("./", "../")):
                        if "://" in repository:
                            url = urllib.parse.urljoin(repository.rstrip("/") + "/", url)
                        elif ":" in repository:
                            raise UnsupportedError("use ssh:// URLs for relative SSH submodules")
                        else:
                            url = str((Path(repository) / url).resolve())
                    url = location(url, Path(repository).parent)
                    from pg_maker.documents import relative_output

                    if not relative_output(path):
                        raise ValidationError("invalid submodule path")
                    target = destination / path
                    if any(p.is_symlink() for p in (target, *target.parents)):
                        raise ValidationError("submodule path traverses a symbolic link")
                    target.mkdir(parents=True, exist_ok=True)
                    locked = self._checkout(url, metadata.split()[2], target, depth + 1)
                    submodules.append({"path": path, **locked})
            return {"commit": commit, "submodules": submodules}

    def resolve(self, source: dict, base: Path) -> dict:
        self.check_cancelled()
        kind = source["kind"]
        spelling = (
            source["repository"]
            if kind == "git"
            else source["location"]
            if kind == "archive"
            else source["path"]
        )
        resolved = location(spelling, base)
        key = content_hash(
            {
                **source,
                "location": resolved,
                **({"snapshot_policy": "git-tree-v1"} if kind == "git" else {}),
            }
        )
        cached = self.store.cached(key)
        # Immutable archives and offline Git refs can use the local resolution index.
        if cached and (self.offline or kind == "archive"):
            self.store.tree(cached["source_hash"])
            if kind == "archive":
                self.store.blob(cached["archive_sha256"])
            return cached
        if self.offline:
            raise PreconditionError("offline inputs are missing", details={"missing": [key]})
        ensure_directory(self.directory)
        with tempfile.TemporaryDirectory(dir=self.directory) as name:
            work = Path(name)
            tree = work / "tree"
            tree.mkdir()
            if kind == "snapshot":
                copy_snapshot(Path(resolved), tree, source["include"], source["exclude"])
                pinned = {
                    "kind": kind,
                    "selection": {
                        "algorithm": "fnmatchcase-posix/v1",
                        "include": source["include"],
                        "exclude": source["exclude"],
                    },
                }
            elif kind == "git":
                pinned = {"kind": kind, **self._checkout(resolved, source["ref"], tree)}
            elif kind == "archive":
                if "://" in resolved:
                    if not resolved.startswith(("https://", "http://")):
                        raise UnsupportedError("archives require HTTP(S) or a local file")
                    archive = work / "download"
                    try:
                        with (
                            urllib.request.urlopen(resolved, timeout=30) as incoming,
                            archive.open("xb") as outgoing,
                        ):
                            if not incoming.url.startswith(("https://", "http://")):
                                raise ValidationError("archive redirect changed transport")
                            while block := incoming.read(1024 * 1024):
                                self.check_cancelled()
                                outgoing.write(block)
                    except OSError as exc:
                        raise PreconditionError("archive download failed") from exc
                else:
                    archive = Path(resolved)
                digest = self.store.put_blob(archive, source["sha256"])
                extract_archive(self.store.blob(digest), tree, source.get("strip_components", 0))
                pinned = {
                    "kind": kind,
                    "archive_sha256": digest,
                    "strip_components": source.get("strip_components", 0),
                }
            else:
                raise ValidationError("unknown source kind")
            self.check_cancelled()
            pinned["source_hash"] = self.store.put_tree(tree)
        self.store.remember(key, pinned)
        return pinned


def verify_postgresql_version(tree: Path, expected: str) -> None:
    configure = tree / "configure.ac"
    if not configure.is_file():
        raise ValidationError("PostgreSQL source lacks configure.ac")
    text = configure.read_text()
    match = re.search(r"AC_INIT\(\[PostgreSQL\],\s*\[([^]]+)\]", text)
    if match is None or match[1] != expected:
        raise ValidationError(
            "PostgreSQL source version differs from manifest",
            details={"expected": expected, "actual": match[1] if match else None},
        )
