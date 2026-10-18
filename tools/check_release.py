"""Check English documentation, local links, versions and built distribution contents."""

from __future__ import annotations

import argparse
import ast
import os
import re
import tarfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

IGNORED = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "build",
    "dist",
    ".tox",
    ".nox",
    "node_modules",
}
LOCAL_DATA = {"operations", "runs", "inputs", "development", "work", "delivery", ".cache"}


def project_files(root: Path):
    for directory, names, files in os.walk(root):
        names[:] = sorted(
            name
            for name in names
            if name not in IGNORED
            and not name.endswith(".egg-info")
            and not (Path(directory) == root and name in LOCAL_DATA)
        )
        for name in sorted(files):
            yield Path(directory, name).relative_to(root)


def check_sources(root: Path, tag: str | None):
    metadata = tomllib.loads((root / "pyproject.toml").read_text())
    version = metadata["project"]["version"]
    module = ast.parse((root / "src/pg_maker/__init__.py").read_text())
    declared = next(
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    )
    failures = []
    if declared != version:
        failures.append("Package version differs from pyproject.toml")
    if tag is not None and tag != "v" + version:
        failures.append(f"Tag {tag!r} does not match v{version}")
    documents = [path for path in project_files(root) if path.suffix.lower() == ".md"]
    for path in documents:
        source = (root / path).read_text(encoding="utf-8")
        if re.search(r"[\u0400-\u04ff]", source):
            failures.append(f"Non-English Cyrillic text in {path}")
        # Inline file links; fenced examples are not navigation links.
        prose = re.sub(r"```.*?```", "", source, flags=re.DOTALL)
        for target in re.findall(r"!?\[[^\]\n]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", prose):
            link = urlsplit(target.strip("<>"))
            if link.scheme or link.netloc or not link.path:
                continue
            destination = (root / path.parent / unquote(link.path)).resolve()
            if not destination.is_relative_to(root) or not destination.exists():
                failures.append(f"Broken or external checkout link: {path}: {target}")
    return version, documents, failures


def check_archives(root: Path, directory: Path, version: str, documents: list[Path]):
    wheels, sdists = list(directory.glob("*.whl")), list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return ["Expected exactly one wheel and one sdist in the distribution directory"]
    failures = []
    package = [
        path for path in project_files(root / "src/pg_maker") if path.suffix not in {".pyc", ".pyo"}
    ]
    with zipfile.ZipFile(wheels[0]) as wheel, tarfile.open(sdists[0], "r:gz") as source:
        wheel_names = set(wheel.namelist())
        members = {item.name: item for item in source.getmembers()}
        source_prefix = f"pg_maker-{version}"
        doc_prefix = f"pg_maker-{version}.data/data/share/doc/pg-maker"

        def compare(path, wheel_path=None):
            expected = (root / path).read_bytes()
            name = source_prefix + "/" + path.as_posix()
            member = members.get(name)
            if member is None or not member.isfile():
                failures.append(f"sdist missing regular file: {path}")
            else:
                stream = source.extractfile(member)
                if stream is None or stream.read() != expected:
                    failures.append(f"sdist content differs: {path}")
            if wheel_path is not None:
                if wheel_path not in wheel_names:
                    failures.append(f"wheel missing: {path}")
                elif wheel.read(wheel_path) != expected:
                    failures.append(f"wheel content differs: {path}")

        for path in documents:
            name = (
                path.relative_to("src").as_posix()
                if path.is_relative_to("src/pg_maker")
                else f"{doc_prefix}/{path.as_posix()}"
            )
            compare(path, name)
        for path in package:
            compare(Path("src/pg_maker") / path, "pg_maker/" + path.as_posix())
        compare(Path("LICENSE"), f"pg_maker-{version}.dist-info/licenses/LICENSE")
        for path in project_files(root):
            if path.parts[0] in {"tests", "tools", ".github"} and path.suffix != ".log":
                compare(path)
        for name in [*wheel_names, *members]:
            parts = Path(name).parts
            if any(p in {".venv", ".git", "__pycache__", ".env", "operations"} for p in parts):
                failures.append(f"Local/generated data in distribution: {name}")
            if name.endswith((".pyc", ".pyo", ".log")):
                failures.append(f"Generated file in distribution: {name}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path)
    parser.add_argument("--tag")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    version, documents, failures = check_sources(root, args.tag)
    if args.dist:
        failures.extend(check_archives(root, args.dist, version, documents))
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"OK: version {version}; {len(documents)} English Markdown files and local links")
    if args.dist:
        print("OK: wheel/sdist documentation, package resources, license and source tooling")


if __name__ == "__main__":
    main()
