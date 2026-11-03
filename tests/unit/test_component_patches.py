import difflib

import pytest

from pg_maker.config import load_config
from pg_maker.errors import ValidationError
from pg_maker.planning.reuse import compilation_inputs
from pg_maker.sources.store import InputStore
from pg_maker.storage import file_hash
from pg_maker.yamlio import load_yaml
from tests.unit.test_planning import project as project_fixture
from tests.unit.test_planning import resolved, write_yaml

project = project_fixture


def configure_patch(project, replacement="fixed\n"):
    source = project / "component-source"
    source.mkdir(exist_ok=True)
    (source / "file.txt").write_text("original\n")
    patch = project / "patches/component.patch"
    patch.write_text(
        "".join(difflib.unified_diff(["original\n"], [replacement], "a/file.txt", "b/file.txt"))
    )
    recipe = project / "components/patched.yaml"
    write_yaml(
        recipe,
        {
            "api_version": "pg_maker/component/v1",
            "kind": "ComponentRecipe",
            "metadata": {"id": "patched", "revision": 1},
            "spec": {
                "type": "pgxs",
                "source": {
                    "kind": "snapshot",
                    "path": "../component-source",
                    "include": ["**"],
                    "exclude": [],
                },
                "source_patches": ["../patches/component.patch"],
                "compatible_postgresql": ">=18,<19",
                "requires": [],
                "conflicts": [],
                "build_dependencies": [],
                "runtime_dependencies": [],
                "make_targets": ["all"],
                "install_targets": ["install"],
                "test_targets": [],
                "required_tests": [],
                "runtime_files": [],
            },
        },
    )
    feature_path = project / "config/features/my-feature.yaml"
    feature = load_yaml(feature_path)
    feature["spec"]["components"] = ["../../components/patched.yaml"]
    write_yaml(feature_path, feature)
    return recipe, patch


def test_component_patch_is_frozen_and_works_offline(project):
    _, patch = configure_patch(project)
    state, lock = resolved(project)
    assert state["status"] == "succeeded", state
    spec = lock["spec"]["config"]["features"][0]["spec"]["components"][0]["spec"]
    store = InputStore(project / "inputs")
    original = store.tree(spec["source_unpatched"]["source_hash"])
    patched = store.tree(spec["source"]["source_hash"])
    assert (original / "file.txt").read_text() == "original\n"
    assert (patched / "file.txt").read_text() == "fixed\n"
    assert (project / "component-source/file.txt").read_text() == "original\n"
    assert file_hash(patch) in lock["spec"]["blobs"].values()
    state, offline = resolved(project, offline=True)
    assert state["status"] == "succeeded", state
    assert offline["spec"]["config"] == lock["spec"]["config"]


def test_patch_change_invalidates_component_without_changing_core(project):
    configure_patch(project)
    state, before = resolved(project)
    assert state["status"] == "succeeded", state
    configure_patch(project, "changed again\n")
    state, after = resolved(project)
    assert state["status"] == "succeeded", state
    identities = []
    for lock in (before, after):
        config = lock["spec"]["config"]
        identities.append(compilation_inputs(lock, config["features"][0], config["variants"][0]))
    assert identities[0][0] == identities[1][0]
    assert identities[0][1]["patched"] != identities[1][1]["patched"]


def test_component_patch_rejects_wrong_context_without_changing_original(project):
    _, patch = configure_patch(project)
    patch.write_text(patch.read_text().replace("-original", "-different"))
    state, _ = resolved(project)
    assert state["status"] == "failed"
    assert (project / "component-source/file.txt").read_text() == "original\n"


def test_component_patch_requires_external_source(project):
    recipe, _ = configure_patch(project)
    component = load_yaml(recipe)
    component["spec"]["type"] = "contrib"
    del component["spec"]["source"]
    write_yaml(recipe, component)
    with pytest.raises(ValidationError, match="external source"):
        load_config(project / "build.yaml")
