"""Real make/Docker/worker recovery with tiny source fixtures, without a database."""

import os
from pathlib import Path

import pytest

from pg_maker.api import MakerService
from pg_maker.config import load_config
from pg_maker.development.session import DevelopmentSession
from pg_maker.errors import PreconditionError
from pg_maker.planning.documents import seal
from pg_maker.runners.docker import current_user
from pg_maker.storage import read_json, write_json
from pg_maker.yamlio import load_yaml
from tests.unit.test_planning import environment, write_yaml
from tests.unit.test_planning import project as project_fixture

pytestmark = pytest.mark.docker

CONFIGURE = r"""#!/bin/sh
set -eu
mkdir -p src/include
: > src/Makefile.global
: > src/include/pg_config.h
for arg do
  case "$arg" in
    --prefix=*) echo "prefix = ${arg#*=}" >> src/Makefile.global ;;
    --with-*|--without-*|--enable-*|--disable-*)
      option=${arg#--}
      action=${option%%-*}
      option=${option#*-}
      name=${option%%=*}
      value=yes
      case "$option" in *=*) value=${option#*=} ;; esac
      case "$action" in without|disable) value=no ;; esac
      case "$name" in nls) test "$value" = no || value=yes ;; esac
      category=enable
      case "$action" in with|without) category=with ;; esac
      key=$(printf '%s' "$name" | tr '-' '_')
      echo "${category}_${key} = $value" >> src/Makefile.global
      macro=
      case "$name" in
        lz4) macro=USE_LZ4 ;; zstd) macro=USE_ZSTD ;;
        pam) macro=USE_PAM ;; cassert) macro=USE_ASSERT_CHECKING ;;
      esac
      if test -n "$macro" && test "$value" != no; then
        echo "#define $macro 1" >> src/include/pg_config.h
      fi ;;
  esac
done
cp "$(dirname "$0")/Makefile.fixture" Makefile
"""

CORE_MAKE = """include src/Makefile.global
.PHONY: all install world-bin install-world-bin
all: core.bin
core.bin: ../source/core.c
\tcp $< $@
\techo core >> compile.log
install: all
\tmkdir -p $(prefix)/bin $(prefix)/lib
\tcp core.bin $(prefix)/bin/postgres
\tprintf '#!/bin/sh\\nexit 0\\n' > $(prefix)/bin/pg_config
\tchmod +x $(prefix)/bin/pg_config
world-bin: all
install-world-bin: install
"""

COMPONENT_MAKE = """prefix = $(shell dirname $(shell dirname $(PG_CONFIG)))
.PHONY: all install
all: stable.o extension.o
stable.o: stable.c
\tcp $< $@
\techo stable >> compile.log
extension.o: extension.c stable.o
\techo extension >> compile.log
\t! grep -q BROKEN $<
\tcp $< $@
install: all
\tmkdir -p $(prefix)/lib
\tcp extension.o $(prefix)/lib/COMPONENT.so
"""


def make_project(tmp_path, image):
    project = project_fixture.__wrapped__(tmp_path)
    write_yaml(
        project / "config/defaults.yaml",
        {
            "spec": {
                "common_changes": [],
                "build": {"jobs": 1},
                "execution": {
                    "build_user": current_user(),
                    "limits": {"min_free_disk_bytes": 1048576},
                },
            }
        },
    )
    source = project / "source"
    (source / "configure").write_text(CONFIGURE)
    (source / "configure").chmod(0o755)
    (source / "Makefile.fixture").write_text(CORE_MAKE)
    (source / "core.c").write_text("fixture core\n")
    feature_path = project / "config/features/my-feature.yaml"
    feature = load_yaml(feature_path)
    for identifier in ("one", "two"):
        extension = project / identifier
        extension.mkdir()
        (extension / "stable.c").write_text("unchanged helper\n")
        (extension / "extension.c").write_text("BROKEN\n" if identifier == "two" else "working\n")
        (extension / "Makefile").write_text(COMPONENT_MAKE.replace("COMPONENT", identifier))
        recipe = {
            "api_version": "pg_maker/component/v1",
            "kind": "ComponentRecipe",
            "metadata": {"id": identifier, "revision": 1},
            "spec": {
                "type": "pgxs",
                "source": {
                    "kind": "snapshot",
                    "path": "../" + identifier,
                    "include": ["**"],
                    "exclude": [],
                },
                "compatible_postgresql": ">=18,<19",
                "requires": [],
                "conflicts": [],
                "build_dependencies": [],
                "runtime_dependencies": [],
                "make_targets": ["all"],
                "install_targets": ["install"],
                "test_targets": [],
                "required_tests": [],
                "runtime_files": ["lib/" + identifier + ".so"],
            },
        }
        write_yaml(project / "components" / (identifier + ".yaml"), recipe)
        feature["spec"]["components"].append("../../components/" + identifier + ".yaml")
    write_yaml(feature_path, feature)
    config = load_config(project / "build.yaml")
    env = environment(config.data)
    for entry in env["spec"]["images"]:
        entry["image_id"] = image
    env = seal("environment", env["spec"])
    write_json(project / "env.json", env)
    service = MakerService(project / "resolve")
    started = service.prepare(
        project / "build.yaml",
        output=project / "lock.json",
        input_store=project / "inputs",
        environments=project / "env.json",
    )
    completed = service.wait(service.directory / started["operation_id"], timeout_seconds=30)
    assert completed["status"] == "succeeded", completed
    return project


def test_failed_component_edit_resume_cache_objects_debug_and_prune(tmp_path):
    image = os.environ.get("PG_MAKER_DEVELOPMENT_IMAGE")
    if not image:
        pytest.skip("set PG_MAKER_DEVELOPMENT_IMAGE to a local image with make and builder user")
    project = make_project(tmp_path, image)
    session = DevelopmentSession(project / "dev")
    opened = session.open(lock=project / "lock.json")
    first = session.build()
    service = MakerService(project / "dev/operations")
    run1 = Path(first["operation_directory"])
    failed = service.wait(run1, timeout_seconds=90)
    assert failed["status"] == "failed", failed
    branch_id = "my-feature.debian13-x64.candidate"
    root1 = run1 / "workspace/build" / branch_id
    assert (root1 / "prefix/lib/one.so").exists()
    assert (root1 / "components/two/stable.o").exists()
    resumed = service.resume(run1, plan_hash=first["plan_hash"])
    assert resumed["attempt"] == 2
    assert service.wait(run1, timeout_seconds=90)["status"] == "failed"
    assert (root1 / "obj/compile.log").read_text().splitlines() == ["core"]
    # Recover into a separate session and cache, as with a failed ordinary build.
    session = DevelopmentSession(project / "recovered")
    opened = session.open(from_run=run1)
    source = Path(opened["trees"]["component:two"]["source"])
    (source / "extension.c").write_text("fixed extension\n")
    session.patch("commit", scope="component:two", identifier="fix-build")
    second = session.build()
    run2 = Path(second["operation_directory"])
    complete = service.wait(run2, timeout_seconds=90)
    assert complete["status"] == "succeeded", complete
    report = read_json(run2 / "workspace/output" / branch_id / "report.json")
    assert report["cache_producer"]["run_id"] == run1.name
    one = next(c for c in report["components"] if c["id"] == "one")
    assert one["cache_producer"]["run_id"] == run1.name
    assert report["incremental_components"] == ["two"]
    root2 = run2 / "workspace/build" / branch_id
    assert (root2 / "components/two/compile.log").read_text().splitlines() == [
        "stable",
        "extension",
        "extension",
    ]
    assert (root2 / "prefix/lib/two.so").read_text() == "fixed extension\n"
    debug = session.execute(["cat", "extension.o"], component="two")
    debug_result = service.wait(Path(debug["operation_directory"]), timeout_seconds=60)
    assert debug_result["status"] == "succeeded", debug_result
    assert Path(debug_result["result"]["stdout_log"]).read_text() == "fixed extension\n"
    cached_debug = session.execute(["cat", "extension.o"], component="one")
    assert (
        service.wait(Path(cached_debug["operation_directory"]), timeout_seconds=60)["status"]
        == "succeeded"
    )
    third = session.build()
    run3 = Path(third["operation_directory"])
    assert service.wait(run3, timeout_seconds=90)["status"] == "succeeded"
    session.pin("000001")
    assert not [f for f in session.prune(keep_last=1)["files"] if f["iteration"] == "000001"]
    session.pin("000001", pinned=False)
    preview = session.prune(keep_last=1)
    assert preview["bytes"] > 0
    session.prune(keep_last=1, apply=preview)
    assert root1.exists() and not root2.exists() and (run3 / "workspace/build").exists()
    assert (source / "extension.c").read_text() == "fixed extension\n"
    with pytest.raises(PreconditionError, match="scratch was cleaned"):
        service.resume(run2, plan_hash=second["plan_hash"])
