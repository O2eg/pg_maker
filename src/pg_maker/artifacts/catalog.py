"""Discover the bounded set of public outputs of an operation."""

from pathlib import Path

from pg_maker.artifacts.manifest import verify_manifest
from pg_maker.documents import relative_output
from pg_maker.errors import PreconditionError
from pg_maker.storage import file_hash, read_json


def operation_artifacts(directory: Path, state: dict) -> dict:
    references = []

    def add(path, role):
        if path.is_file() and not path.is_symlink():
            references.append(
                {
                    "path": str(path),
                    "role": role,
                    "sha256": file_hash(path),
                    "size_bytes": path.stat().st_size,
                }
            )

    add(directory / "state.json", "operation_state")
    add(directory / "events.jsonl", "operation_events")
    for path in sorted((directory / "workspace/output").glob("*/artifact-manifest.json")):
        manifest = read_json(path)
        verify_manifest(path.parent, manifest)
        add(path, "artifact_manifest")
        for artifact in manifest["artifacts"]:
            references.append({**artifact, "path": str(path.parent / artifact["path"])})
        for name, role in (
            ("report.json", "report"),
            ("report.html", "report_html"),
            ("verification.json", "verification"),
            ("docs-manifest.json", "documentation_manifest"),
        ):
            add(path.parent / name, role)
    result = state.get("result") or {}
    for artifact in result.get("artifacts", []):
        relative = artifact["path"]
        if not relative_output(relative):
            raise PreconditionError("operation output reference must be relative")
        path = directory / relative
        if (
            any(p.is_symlink() for p in (path, *path.parents))
            or file_hash(path) != artifact["sha256"]
        ):
            raise PreconditionError("operation output changed")
        add(path, "operation_output")
    if result.get("document"):
        path = Path(result["document"])
        if file_hash(path) != result["sha256"]:
            raise PreconditionError("operation output changed")
        add(path, "operation_output")
    for report in result.get("reports", []):
        if file_hash(Path(report["path"])) != report["sha256"]:
            raise PreconditionError("operation report changed")
        if report["path"] not in {r["path"] for r in references}:
            add(Path(report["path"]), "report")
    return {
        "operation_id": state["operation_id"],
        "status": state["status"],
        "qualification": result.get("qualification", "unverified"),
        "artifacts": references,
    }
