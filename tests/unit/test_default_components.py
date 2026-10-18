import re
from pathlib import Path

import pytest
import yaml

from pg_maker.assets import initialize_project
from pg_maker.config import load_config
from pg_maker.documentation.config import references
from pg_maker.documentation.xml import parse
from pg_maker.errors import ValidationError
from pg_maker.storage import file_hash


def select(root, ids):
    path = root / "config/features/my-feature.yaml"
    feature = yaml.safe_load(path.read_text())
    feature["spec"]["components"] = ["../../components/" + name + ".yaml" for name in ids]
    path.write_text(yaml.safe_dump(feature, sort_keys=False))


@pytest.mark.parametrize("version", ["14.24", "15.1", "16.1", "17.5", "18.1", "19beta4"])
def test_default_components_dependency_order_pins_and_documentation(tmp_path, version):
    initialize_project(tmp_path, with_doc=True)
    path = tmp_path / "build.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["spec"]["postgresql"]["version"] = version
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    loaded = load_config(path)
    components = loaded.data["features"][0]["spec"]["components"]
    assert [c["metadata"]["id"] for c in components] == [
        "pg-stat-statements",
        "pg-stat-kcache",
        "pg-wait-sampling",
    ]
    assert components[1]["spec"]["requires"] == ["pg-stat-statements"]
    for component in components[1:]:
        assert re.fullmatch("[0-9a-f]{40}", component["spec"]["source"]["ref"])
        assert component["spec"]["test_targets"] == ["installcheck"]
    assert not loaded.data["defaults"]["validation"]["run_db_tests"]
    assets = references(loaded.data)
    assert len(assets) == 2
    for asset in assets:
        source = loaded.bindings[asset["input"]]
        assert file_hash(Path(source)) == asset["sha256"]
        nodes = parse(Path(source).read_text())
        assert nodes[0].tag == "sect1"
        assert any(n.tag == "programlisting" for n in nodes)


def test_component_selection_controls_docs_and_requires_dependency(tmp_path):
    initialize_project(tmp_path, with_doc=True)
    select(tmp_path, ["pg-stat-kcache"])
    with pytest.raises(ValidationError, match="missing dependency"):
        load_config(tmp_path / "build.yaml")
    select(tmp_path, ["pg-wait-sampling"])
    loaded = load_config(tmp_path / "build.yaml")
    assert len(references(loaded.data)) == 1
    assert "pg-wait-sampling" in references(loaded.data)[0]["input"]
    assert references(load_config(tmp_path / "build.yaml", with_doc=False).data) == []
    select(tmp_path, [])
    loaded = load_config(tmp_path / "build.yaml")
    assert loaded.data["features"][0]["spec"]["components"] == []
    assert references(loaded.data) == []


def test_component_manual_changes_are_pinned_as_project_inputs(tmp_path):
    initialize_project(tmp_path, with_doc=True)
    before = references(load_config(tmp_path / "build.yaml").data)
    manual = tmp_path / "documentation/pg-stat-kcache.sgml"
    manual.write_text(manual.read_text().replace("CPU consumption", "CPU usage"))
    after = references(load_config(tmp_path / "build.yaml").data)
    assert before[0]["sha256"] != after[0]["sha256"]
    assert before[1] == after[1]
