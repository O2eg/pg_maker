"""Apply locked component patches to a private copy before freezing the source tree."""

import shutil
import tempfile
from pathlib import Path

from pg_maker.changes.apply import apply_patch
from pg_maker.errors import MakerError


def patch_component_source(spec, store, directory, *, check=lambda: None):
    patches = spec.get("source_patches", [])
    if not patches:
        return
    check()
    original = dict(spec["source"])
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="component-patches-", dir=directory) as temporary:
        tree = Path(temporary) / "source"
        shutil.copytree(store.tree(original["source_hash"]), tree, symlinks=True)
        for reference in patches:
            check()
            patch = store.blob(reference["sha256"]).read_text(encoding="utf-8")
            try:
                apply_patch(tree, patch)
            except MakerError as exc:
                exc.details.update(patch_input=reference["input"], patch_sha256=reference["sha256"])
                raise
        check()
        patched_hash = store.put_tree(tree)
    spec["source_unpatched"] = original
    spec["source"] = {**original, "source_hash": patched_hash}
