"""Strict, transactional text changes on private source copies; no shell or fuzz."""

from __future__ import annotations

import difflib
import os
import re
import shutil
from pathlib import Path

from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.sources.store import inventory
from pg_maker.storage import content_hash, file_hash, write_json
from pg_maker.yamlio import load_yaml

SOCKET_DEFAULT_LINE = (
    '#define DEFAULT_PGSOCKET_DIR "/var/run/postgresql" /* Shared by server and libpq. */\n'
)


def target_path(root: Path, relative: str) -> Path:
    if not relative_output(relative):
        raise ValidationError("change path must be relative", details={"path": relative})
    path = root / relative
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValidationError(
            "change target must not traverse a symlink", details={"path": relative}
        )
    if path.exists() and not path.is_file():
        raise ValidationError("change target must be a regular file", details={"path": relative})
    return path


def insert_text(root: Path, document: dict) -> None:
    path = target_path(root, document["target_file"])
    if not path.is_file():
        raise PreconditionError("insertion target is missing")
    if document.get("input_sha256") and file_hash(path) != document["input_sha256"]:
        raise PreconditionError("insertion input hash mismatch")
    text = path.read_bytes().decode("utf-8")
    for mod in document["modifications"]:
        top, bot, addition = mod["top"], mod["bot"], mod["txt"]
        if not top or not bot or any(c in top + bot for c in "\r\n") or not addition:
            raise ValidationError("insertion requires nonempty, single-line anchors and text")
        lines = text.splitlines(keepends=True)
        tops = [i for i, line in enumerate(lines) if top in line]
        pairs = []
        for i, start in enumerate(tops):
            end = tops[i + 1] if i + 1 < len(tops) else len(lines)
            bottom = next((j for j in range(start + 1, end) if bot in lines[j]), None)
            if bottom is None:
                raise PreconditionError("insertion bottom anchor is missing")
            pairs.append((start, bottom))
        if len(pairs) != mod.get("expect_matches", 1):
            raise PreconditionError("insertion anchor count mismatch")
        for start, end in reversed(pairs):
            if addition in "".join(lines[start + 1 : end]):
                raise PreconditionError("insertion already present without a receipt")
            index = start + 1 if mod["alignment"] == "top" else end
            if index and not lines[index - 1].endswith("\n"):
                raise PreconditionError("insertion anchor has no line terminator")
            lines.insert(index, addition if addition.endswith("\n") else addition + "\n")
        text = "".join(lines)
    path.write_bytes(text.encode("utf-8"))


def socket_patch(source: Path) -> str:
    """Pin an exact-offset patch for this source, using one unique full-line anchor."""
    relative = "src/include/pg_config_manual.h"
    path = target_path(source, relative)
    if not path.is_file():
        raise PreconditionError("socket default header is missing")
    upstream = '#define DEFAULT_PGSOCKET_DIR  "/tmp"\n'
    configured = '#define DEFAULT_PGSOCKET_DIR  "/var/run/postgresql"\n'
    lines = path.read_text().splitlines(keepends=True)
    if sum(lines.count(line) for line in (upstream, configured, SOCKET_DEFAULT_LINE)) != 1:
        raise PreconditionError("builtin socket patch requires one exact upstream/default line")
    before = [upstream if line == SOCKET_DEFAULT_LINE else line for line in lines]
    after = [SOCKET_DEFAULT_LINE if line in (upstream, configured) else line for line in lines]
    return "".join(difflib.unified_diff(before, after, "a/" + relative, "b/" + relative, n=0))


def apply_patch(root: Path, patch: str) -> None:
    """Unified UTF-8 diffs, exact offsets/context, additions/deletions; reject other formats."""
    lines = patch.splitlines(keepends=True)
    if lines and re.match(r"From [0-9a-f]{40,64} Mon Sep 17 00:00:00 2001", lines[0]):
        first = next((i for i, line in enumerate(lines) if line.startswith("diff --git ")), None)
        if first is None:
            raise ValidationError("format-patch contains no diff")
        lines = lines[first:]
        # git's mail signature is outside the diff; arbitrary trailing input is rejected.
        if (
            len(lines) >= 2
            and lines[-2] == "-- \n"
            and re.fullmatch(r"[0-9][A-Za-z0-9.+-]*\n?", lines[-1])
        ):
            lines = lines[:-2]
        elif len(lines) >= 3 and not lines[-1].strip() and lines[-3] == "-- \n":
            if not re.fullmatch(r"[0-9][A-Za-z0-9.+-]*\n?", lines[-2]):
                raise ValidationError("invalid format-patch signature")
            lines = lines[:-3]
    index, applied = 0, 0
    mode, mode_kind = None, None
    while index < len(lines):
        if lines[index].startswith("diff --git "):
            match = re.fullmatch(r"diff --git a/(\S+) b/(\S+)\n?", lines[index])
            if not match or match[1] != match[2] or not relative_output(match[1]):
                raise UnsupportedError("patch requires safe unquoted paths without renames")
            name = match[1]
            mode, mode_kind = None, None
            index += 1
            while index < len(lines) and not lines[index].startswith(("--- ", "diff --git ")):
                line = lines[index].rstrip("\n")
                metadata = re.fullmatch(r"(new file|deleted file) mode (100644|100755)", line)
                if metadata and mode is None:
                    mode_kind, mode = metadata[1], int(metadata[2], 8) & 0o777
                elif re.fullmatch(r"index [0-9a-f]+\.\.[0-9a-f]+(?: (?:100644|100755))?", line):
                    pass
                else:
                    raise UnsupportedError("binary, rename, copy and mode changes are unsupported")
                index += 1
            if index == len(lines) or lines[index].startswith("diff --git "):
                if mode_kind is None:
                    raise ValidationError("patch has no file content")
                old, new = (
                    ("/dev/null", "b/" + name)
                    if mode_kind == "new file"
                    else ("a/" + name, "/dev/null")
                )
                lines[index:index] = [f"--- {old}\n", f"+++ {new}\n", "@@ -0,0 +0,0 @@\n"]
            continue
        if not lines[index].strip():
            index += 1
            continue
        if not lines[index].startswith("--- ") or index + 1 >= len(lines):
            raise UnsupportedError(
                "only unified text patches without mode/rename changes are supported"
            )
        old = lines[index][4:].split("\t")[0].rstrip("\r\n")
        if not lines[index + 1].startswith("+++ "):
            raise ValidationError("patch lacks new file header")
        new = lines[index + 1][4:].split("\t")[0].rstrip("\r\n")
        for name in (old, new):
            if name != "/dev/null" and (
                not name.startswith(("a/", "b/")) or not relative_output(name[2:])
            ):
                raise ValidationError("patch requires safe a/ and b/ paths")
        if old != "/dev/null" and new != "/dev/null" and old[2:] != new[2:]:
            raise UnsupportedError("patch renames are not supported")
        if (
            (mode_kind == "new file" and old != "/dev/null")
            or (mode_kind == "deleted file" and new != "/dev/null")
            or old == new == "/dev/null"
        ):
            raise ValidationError("patch mode metadata differs from file headers")
        path = target_path(root, (new if old == "/dev/null" else old)[2:])
        if old == "/dev/null":
            if path.exists():
                raise PreconditionError("patch addition would overwrite an existing file")
            original = []
        else:
            if not path.is_file():
                raise PreconditionError("patch input is missing")
            if mode is not None and bool(path.stat().st_mode & 0o111) != bool(mode & 0o111):
                raise PreconditionError("patch input executable mode mismatch")
            original = path.read_bytes().decode("utf-8").splitlines(keepends=True)
        index += 2
        output, cursor, hunks = [], 0, 0
        while index < len(lines) and lines[index].startswith("@@ "):
            match = re.fullmatch(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", lines[index]
            )
            if not match:
                raise ValidationError("invalid patch hunk")
            old_start, old_n, new_start, new_n = (
                int(v) if v is not None else 1 for v in match.groups()
            )
            start = old_start - 1 if old_n else old_start
            if start < cursor or start > len(original):
                raise PreconditionError("patch hunk position mismatch")
            output.extend(original[cursor:start])
            if len(output) != (new_start - 1 if new_n else new_start):
                raise PreconditionError("patch output position mismatch")
            before, after = [], []
            index += 1
            while index < len(lines) and (len(before) < old_n or len(after) < new_n):
                line = lines[index]
                if not line or line[0] not in " +-":
                    raise ValidationError("invalid patch hunk body")
                value = line[1:]
                index += 1
                if index < len(lines) and lines[index].startswith("\\ No newline at end of file"):
                    value = value.removesuffix("\n")
                    index += 1
                if line[0] in " -":
                    before.append(value)
                if line[0] in " +":
                    after.append(value)
            if (
                len(before) != old_n
                or len(after) != new_n
                or original[start : start + old_n] != before
            ):
                raise PreconditionError("patch context mismatch; offsets and fuzz are forbidden")
            output.extend(after)
            cursor = start + old_n
            hunks += 1
        if not hunks:
            raise ValidationError("patch has no hunks")
        output.extend(original[cursor:])
        if new == "/dev/null":
            if output:
                raise ValidationError("file deletion leaves content")
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes("".join(output).encode("utf-8"))
            if old == "/dev/null" and mode is not None:
                path.chmod(mode)
        applied += 1
        mode, mode_kind = None, None
    if not applied:
        raise ValidationError("empty patch")


def change_receipt(before: Path, after: Path, identifier: str) -> dict:
    left, right = inventory(before), inventory(after)
    a = {e["path"]: e for e in left["entries"]}
    b = {e["path"]: e for e in right["entries"]}
    changed = [p for p in sorted(a.keys() | b.keys()) if a.get(p) != b.get(p)]
    diffs = []
    for p in changed:
        if any(e.get("type") not in {None, "file"} for e in (a.get(p, {}), b.get(p, {}))):
            continue
        old = (before / p).read_text().splitlines(keepends=True) if p in a else []
        new = (after / p).read_text().splitlines(keepends=True) if p in b else []
        diffs.extend(difflib.unified_diff(old, new, fromfile="a/" + p, tofile="b/" + p))
    return {
        "id": identifier,
        "input_hash": content_hash(left),
        "output_hash": content_hash(right),
        "changed_paths": changed,
        "diff": "".join(diffs),
    }


def apply_changes(
    source: Path, feature: dict, lock: dict, store, branch: str, receipts: Path, check=lambda: None
) -> list[dict]:
    changes = []
    if lock["spec"]["config"]["defaults"]["common_changes"]:
        changes.append(("builtin/unix-socket-directory", None))
    if branch == "candidate":
        changes.extend((c["metadata"]["id"], c) for c in feature["spec"]["changes"])
    results = []
    receipts.mkdir(parents=True, exist_ok=True)
    for number, (identifier, change) in enumerate(changes):
        check()
        temporary = source.with_name("source.change-" + str(number))
        shutil.copytree(source, temporary, symlinks=True)
        if change is None:
            path = temporary / "src/include/pg_config_manual.h"
            value = SOCKET_DEFAULT_LINE.rstrip("\n")
            if value not in path.read_text().splitlines():
                digest = lock["spec"]["blobs"][identifier]
                apply_patch(temporary, store.blob(digest).read_text())
            if value not in path.read_text().splitlines():
                raise PreconditionError("builtin socket postcondition failed")
        else:
            for operation in change["spec"]["operations"]:
                check()
                blob = store.blob(operation["file"]["sha256"])
                if operation["kind"] == "patch":
                    apply_patch(temporary, blob.read_text())
                else:
                    insert_text(temporary, load_yaml(blob, "insertions"))
            expected = change["spec"].get("postconditions", {}).get("socket_directory")
            if expected is not None:
                actual = re.findall(
                    r'^#define DEFAULT_PGSOCKET_DIR\s+"([^"]+)"',
                    (temporary / "src/include/pg_config_manual.h").read_text(),
                    re.M,
                )
                if [value for value in actual if value] != [expected]:
                    raise PreconditionError("socket directory postcondition failed")
        receipt = change_receipt(source, temporary, identifier)
        receipt["status"] = "applied" if receipt["changed_paths"] else "already_satisfied"
        if change is not None and not receipt["changed_paths"]:
            raise PreconditionError("custom change has no effect")
        check()
        backup = source.with_name("source.before-" + str(number))
        os.replace(source, backup)
        os.replace(temporary, source)
        write_json(receipts / (str(number) + ".json"), receipt)
        shutil.rmtree(backup)
        results.append(receipt)
    return results
