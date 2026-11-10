"""Standalone target identity, dependency policies and evidence failure boundaries."""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.extensions.config import BUILTINS, initialize, load_project, plan, seal
from pg_maker.extensions.packages import identity
from pg_maker.extensions.verification import verify_extension_output
from pg_maker.models import ExtensionRequest, parse_request
from pg_maker.planning.documents import implementation_identity
from pg_maker.storage import content_hash, file_hash, write_json


@pytest.fixture
def project(tmp_path):
    initialize(tmp_path / "project", extensions=list(BUILTINS))
    return tmp_path / "project/extensions.yaml"


def test_rpm_inventory_compares_contents_independent_of_directory_order(tmp_path):
    from pg_maker.extensions.container import file_inventory, verify_rpm_inventory

    (tmp_path / "module").mkdir()
    (tmp_path / "module/part.bc").write_bytes(b"part")
    (tmp_path / "module.index.bc").write_bytes(b"index")
    inventory = file_inventory(tmp_path)
    query = "\n".join(
        f"/{e['path']}\t{e['size']}\t{0o100000 | e['mode']}\t{e['sha256'][7:]}\t\troot\troot"
        for e in sorted(inventory, key=lambda e: e["path"])
    )
    verify_rpm_inventory(inventory, query)
    with pytest.raises(RuntimeError, match="root-owned"):
        verify_rpm_inventory(inventory, query.replace("root", "builder"))
    with pytest.raises(RuntimeError, match="differs"):
        verify_rpm_inventory(inventory, query.replace(inventory[0]["sha256"][7:], "0" * 64))


def test_builtin_projects_separate_source_release_from_sql_version(project):
    config = load_project(project)
    wait = config["extensions"][0]
    assert wait["packaging"]["version"] == "1.1.11"
    assert wait["spec"]["sql_extension_versions"] == {"pg_wait_sampling": "1.1"}
    assert config["validation"]["run_tests"] is True
    assert config["target"]["dependency_policy"] == "major"
    assert config["execution"]["min_free_disk_bytes"] == 5 * 1024**3


@pytest.mark.parametrize(
    "fmt,provider", [("deb", "community"), ("deb", "profile"), ("rpm", "community")]
)
def test_native_packages_bind_to_target_and_extension_versions(project, fmt, provider):
    config = load_project(project)
    target = config["target"]
    target.update(
        format=fmt,
        provider=provider,
        dependency_policy="exact" if provider == "profile" else "major",
    )
    if fmt == "rpm":
        target["os"] = {"id": "rocky", "version": "9"}
        target["server_package"] = "postgresql18-server"
    elif provider == "profile":
        target["server_package"] = "postgresql-18-yc-clone"
    sdk = {
        "postgresql_version": "18.6",
        "server_package": {"name": target["server_package"], "version": "18.6-1.deb13+extensions1"},
    }
    result = identity(config["extensions"][0], target, sdk)
    assert result["version"].startswith("1.1.11-1.")
    assert result["name"].startswith("postgresql-18-pg-wait-sampling")
    assert result["name"].endswith("-yc-clone") == (provider == "profile")
    assert all(target["server_package"] in dep for dep in result["dependencies"])
    if provider == "profile":
        assert result["dependencies"] == ["postgresql-18-yc-clone (= 18.6-1.deb13+extensions1)"]
    elif fmt == "deb":
        assert result["dependencies"] == ["postgresql-18 (>= 18.6)", "postgresql-18 (<< 19~)"]
    else:
        assert result["dependencies"] == ["postgresql18-server >= 18.6", "postgresql18-server < 19"]
        assert result["filename"].endswith(".el9.x86_64.rpm")


def test_profile_requires_real_packages_and_never_treats_major_as_sdk(tmp_path):
    with pytest.raises(ValidationError, match="actual DEBs"):
        initialize(tmp_path / "missing", extensions=["pg_repack"], profile="yc-18.6-icu70")


def test_changed_database_input_is_rejected_before_operation(tmp_path):
    packages = tmp_path / "packages"
    packages.mkdir()
    package = packages / "fixture.deb"
    package.write_bytes(b"original")
    initialized = initialize(
        tmp_path / "project", extensions=["pg_repack"], profile="yc-18.6-icu70", packages=packages
    )
    package.write_bytes(b"changed")
    from pathlib import Path

    with pytest.raises(PreconditionError, match="package changed"):
        load_project(Path(initialized["manifest"]))


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "1.0\nRequires: fake"),
        ("package_name", "bad;command"),
        ("license", "%{execute:evil}"),
    ],
)
def test_package_metadata_cannot_inject_native_control_fields(project, field, value):
    document = yaml.safe_load(project.read_text())
    document["spec"]["extensions"][0][field] = value
    project.write_text(yaml.safe_dump(document))
    with pytest.raises(ValidationError):
        load_project(project)


def test_extension_operations_use_durable_typed_worker_contract(project, tmp_path):
    config = load_project(project)
    request = ExtensionRequest(
        "extension_prepare", config, str(tmp_path / "lock.json"), str(tmp_path / "inputs")
    )
    parsed = parse_request(request.to_document())
    assert parsed == request
    validate_document(request.to_document(), "operation-input")
    changed = request.to_document()
    changed["unrecognized"] = True
    with pytest.raises(ValidationError):
        parse_request(changed)


def test_extension_plan_never_schedules_core_compilation(project):
    config = load_project(project)
    locked = example_lock(config)
    planned = plan(locked)
    kinds = [s["kind"] for s in planned["spec"]["steps"]]
    assert kinds.count("build") == 3 and kinds.count("package") == 3
    assert "test-packages" in kinds
    assert "test-source" in kinds
    assert "compile_core" not in kinds
    changed = copy.deepcopy(locked)
    changed["spec"]["config"]["validation"]["run_tests"] = False
    with pytest.raises(PreconditionError, match="hash changed"):
        plan(changed)


def test_export_verification_detects_package_and_report_tampering(tmp_path):
    package = tmp_path / "fixture.deb"
    package.write_bytes(b"original")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"passed": True}))
    manifest = {"evidence": [{"path": p.name, "sha256": file_hash(p)} for p in (package, report)]}
    manifest["manifest_hash"] = content_hash(manifest)
    write_json(tmp_path / "extension-manifest.json", manifest)
    verify_extension_output(tmp_path)
    report.write_text(json.dumps({"passed": False}))
    with pytest.raises(PreconditionError, match="evidence changed"):
        verify_extension_output(tmp_path)


def example_lock(config):
    config = copy.deepcopy(config)
    zero = "sha256:" + "0" * 64
    for component in config["extensions"]:
        component.pop("base")
        component["spec"]["source"] = {
            "kind": "archive",
            "archive_sha256": zero,
            "strip_components": 1,
            "source_hash": zero,
        }
    return seal(
        "lock",
        {
            "config": config,
            "implementation": implementation_identity(),
            "uid": 1000,
            "images": {"build": zero, "test": zero},
            "base_image": zero,
            "runtime_repository": [],
            "target": {
                "os": config["target"]["os"],
                "layout": {},
                "postgresql_version": "18.6",
                "server_package": {
                    "name": "postgresql-18",
                    "version": "18.6-1",
                    "architecture": "amd64",
                },
                "sdk_files": [],
                "sql_extensions": {},
            },
        },
    )


def test_unknown_lock_fields_fail_even_when_rehashed(project):
    from pg_maker.extensions.config import verify

    locked = example_lock(load_project(project))
    locked["spec"]["ignored"] = "must not be silently ignored"
    locked = seal("lock", locked["spec"])
    with pytest.raises(ValidationError):
        verify(locked, "lock")


def test_export_rejects_untracked_payload(tmp_path):
    manifest = {"evidence": []}
    manifest["manifest_hash"] = content_hash(manifest)
    write_json(tmp_path / "extension-manifest.json", manifest)
    (tmp_path / "leftover.so").write_bytes(b"stale")
    with pytest.raises(PreconditionError, match="inventory changed"):
        verify_extension_output(tmp_path)


def test_prepared_images_reuse_only_unchanged_provisioning():
    from pg_maker.config import resource_path
    from pg_maker.extensions.execution import preparation_identity

    source = resource_path("extensions", "container.py").read_text()
    original = preparation_identity(source)
    changed = source.replace('"/work/output/package-tests.json"', '"/work/output/new-tests.json"')
    assert changed != source
    assert preparation_identity(changed) == original
    assert (
        preparation_identity(source.replace('"build-essential"', '"different-compiler"'))
        != original
    )


def test_dependency_graph_rejects_missing_provider_and_cycles(project):
    from pg_maker.extensions.config import order_extensions

    config = load_project(project)
    with pytest.raises(PreconditionError, match="unresolved"):
        order_extensions(config, {"sql_extensions": {}})
    sdk = {"sql_extensions": {"pg_stat_statements": "fixture"}}
    ordered = order_extensions(config, sdk)
    assert len(ordered) == 3
    config["extensions"][0]["spec"]["requires"] = ["pg_repack"]
    config["extensions"][2]["spec"]["requires"] = ["pg_wait_sampling"]
    with pytest.raises(ValidationError, match="cycle"):
        order_extensions(config, sdk)


def test_repack_frontend_link_dependencies_are_platform_specific(tmp_path):
    for platform, expected, unwanted in (
        ("debian-13", "libzstd-dev", "libzstd-devel"),
        ("rocky-10", "libzstd-devel", "libzstd-dev"),
    ):
        root = tmp_path / platform
        initialize(root, extensions=["pg_repack", "pg_wait_sampling"], platform=platform)
        config = load_project(root / "extensions.yaml")
        repack, wait = config["extensions"]
        assert expected in repack["spec"]["build_dependencies"]
        assert unwanted not in repack["spec"]["build_dependencies"]
        assert not wait["spec"]["build_dependencies"]


def test_resume_retains_objects_but_never_accepts_unlocked_source_edits(tmp_path):
    from pg_maker.extensions.packages import verify_retained_sources

    baseline, source = tmp_path / "baseline", tmp_path / "source"
    baseline.mkdir()
    (baseline / "extension.c").write_text("original")
    shutil.copytree(baseline, source)
    (source / "extension.o").write_bytes(b"retained generated object")
    verify_retained_sources(source, baseline)
    (source / "extension.c").write_text("changed")
    with pytest.raises(PreconditionError, match="retained source differs"):
        verify_retained_sources(source, baseline)


@pytest.mark.parametrize("path", ["/tmp/build/lib", "/unrelated/lib", "$ORIGIN", ""])
def test_rpm_rpath_exception_never_accepts_unapproved_paths(path):
    from pg_maker.extensions.container import validate_runpaths

    with pytest.raises(RuntimeError, match="unapproved"):
        validate_runpaths(
            "(RUNPATH) Library runpath: [" + path + "]",
            {"libdir": "/usr/pgsql-18/lib", "pkglibdir": "/usr/pgsql-18/lib"},
        )


def test_rpm_rpath_accepts_only_the_locked_sdk_directory():
    from pg_maker.extensions.container import validate_runpaths

    assert validate_runpaths(
        "(RUNPATH) Library runpath: [/usr/pgsql-18/lib]",
        {"libdir": "/usr/pgsql-18/lib", "pkglibdir": "/usr/pgsql-18/lib"},
    ) == {"/usr/pgsql-18/lib"}


@pytest.mark.skipif(not shutil.which("dpkg-deb"), reason="native dpkg-deb required")
def test_yc_split_preserves_contrib_and_every_unrelated_file(tmp_path, monkeypatch):
    from pg_maker.extensions import container
    from pg_maker.packaging.deb import inspect_deb

    prefix = "opt/postgresql-yc-clone/18/"
    control = prefix + "share/postgresql/extension/"
    payloads = {
        "postgresql-18-yc-clone": {
            prefix + "lib/postgresql/auto_explain.so": b"contrib must survive",
            prefix + "bin/postgres": b"server must survive",
            prefix + "lib/postgresql/pg_repack.so": b"repack module",
            prefix + "lib/postgresql/pg_stat_kcache.so": b"kcache module",
            control + "pg_repack.control": b"default_version = '1.5.3'",
            control + "pg_repack--1.5.3.sql": b"repack sql",
            control + "pg_stat_kcache.control": b"default_version = '2.3.1'",
            control + "pg_stat_kcache--2.3.1.sql": b"kcache sql",
        },
        "postgresql-client-18-yc-clone": {
            prefix + "bin/pg_repack": b"repack client",
            prefix + "bin/psql": b"client must survive",
        },
        "libpq5-yc-clone": {prefix + "lib/libpq.so.5.18": b"library must survive"},
    }
    inputs = []
    for name, files in payloads.items():
        tree = tmp_path / name
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN/control").write_text(
            f"Package: {name}\nVersion: 18.6-1.deb13\nArchitecture: amd64\n"
            "Maintainer: Test <test@example.invalid>\nDescription: migration fixture\n"
        )
        for relative, value in files.items():
            path = tree / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        package = tmp_path / (name + ".deb")
        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", "-Zxz", str(tree), str(package)],
            check=True,
            capture_output=True,
        )
        inputs.append(package)
    before = {str(p): file_hash(p) for p in inputs}

    def mapped_path(*parts):
        path = Path(*parts)
        for original, replacement in (
            ("/work/output", tmp_path / "output"),
            ("/tmp/extension-target-split", tmp_path / "split"),
        ):
            if path.is_relative_to(original):
                return replacement / path.relative_to(original)
        return path

    monkeypatch.setattr(container, "Path", mapped_path)
    job = {
        "config": {
            "target": {
                "provider": "profile",
                "format": "deb",
                "major": 18,
                "server_package": "postgresql-18-yc-clone",
                "split_bundled": True,
            },
            "extensions": [{"spec": {"sql_extension": n}} for n in ("pg_stat_kcache", "pg_repack")],
        }
    }
    outputs = container.split_database(job, inputs)
    assert before == {str(p): file_hash(p) for p in inputs}
    for old, new in zip(inputs, outputs, strict=True):
        original, split = inspect_deb(old), inspect_deb(new)
        expected = {
            e["path"]: e
            for e in original["entries"]
            if e["type"] != "directory"
            and not any(n in e["path"] for n in ("pg_repack", "pg_stat_kcache"))
        }
        actual = {e["path"]: e for e in split["entries"] if e["type"] != "directory"}
        assert actual == expected
        assert split["control"]["Version"].startswith("18.6-1.deb13+extensions2.")
    both_version = inspect_deb(outputs[0])["control"]["Version"]
    full_hash = container.migration_hash(job, inputs)
    assert container.migration_hash(job, list(reversed(inputs))) == full_hash
    job["config"]["extensions"] = [{"spec": {"sql_extension": "pg_repack"}}]
    repack_outputs = container.split_database(job, inputs)
    repack_server = inspect_deb(repack_outputs[0])
    assert repack_server["control"]["Version"] != both_version
    assert container.migration_hash(job, inputs) != full_hash
    paths = {entry["path"] for entry in repack_server["entries"]}
    assert prefix + "lib/postgresql/pg_stat_kcache.so" in paths
    assert prefix + "lib/postgresql/pg_repack.so" not in paths
    assert before == {str(p): file_hash(p) for p in inputs}


def test_migration_selection_is_part_of_sdk_cache_key(project):
    from pg_maker.extensions.execution import environment_key

    config = load_project(project)
    config["target"]["split_bundled"] = True
    all_modules = environment_key(config, ["same-build-deps"], [], 1000)
    config["extensions"] = [
        e for e in config["extensions"] if e["spec"]["sql_extension"] == "pg_repack"
    ]
    repack = environment_key(config, ["same-build-deps"], [], 1000)
    assert repack != all_modules
    config["extensions"].reverse()
    assert environment_key(config, ["same-build-deps"], [], 1000) == repack


def test_exact_sdk_changes_also_change_extension_package_version(project):
    config = load_project(project)
    target = config["target"]
    target.update(provider="profile", dependency_policy="exact")
    sdk = {
        "postgresql_version": "18.6",
        "server_package": {
            "name": "postgresql-18-yc-clone",
            "version": "18.6-1.deb13+extensions2.aaaa",
        },
    }
    first = identity(config["extensions"][0], target, sdk)
    sdk["server_package"]["version"] = "18.6-1.deb13+extensions2.bbbb"
    second = identity(config["extensions"][0], target, sdk)
    assert first["name"] == second["name"]
    assert first["filename"] != second["filename"]
    assert first["version"] != second["version"]
    assert first["dependencies"] != second["dependencies"]


def test_test_cluster_cleanup_preserves_sources_and_unrelated_files(tmp_path):
    from pg_maker.extensions.container import remove_test_tablespaces

    workspace = tmp_path / "build"
    data = workspace / "cluster/data"
    links = data / "pg_tblspc"
    links.mkdir(parents=True)
    tablespace = workspace / "components/pg_repack/tablespaces/ts1"
    version = tablespace / "PG_18_202506291"
    version.mkdir(parents=True)
    (version / "old-test-relation").write_text("old data")
    (tablespace / "keep.txt").write_text("unrelated")
    source = workspace / "components/pg_repack/extension.c"
    source.write_text("source")
    (links / "12345").symlink_to(tablespace, target_is_directory=True)
    remove_test_tablespaces(data, workspace)
    assert not version.exists()
    assert source.read_text() == "source"
    assert (tablespace / "keep.txt").read_text() == "unrelated"
    remove_test_tablespaces(data, workspace)


def test_test_cluster_cleanup_refuses_external_tablespace(tmp_path):
    from pg_maker.extensions.container import remove_test_tablespaces

    workspace = tmp_path / "build"
    data = workspace / "cluster/data"
    (data / "pg_tblspc").mkdir(parents=True)
    outside = tmp_path / "external/PG_18_202506291"
    outside.mkdir(parents=True)
    (data / "pg_tblspc/12345").symlink_to(outside.parent, target_is_directory=True)
    with pytest.raises(RuntimeError, match="outside the owned workspace"):
        remove_test_tablespaces(data, workspace)
    assert outside.is_dir()


def test_container_timeout_preserves_both_output_streams(capsys):
    import sys

    from pg_maker.extensions.container import run

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        run(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys,time;print('partial-out',flush=True);"
                "print('partial-err',file=sys.stderr,flush=True);time.sleep(10)",
            ],
            timeout=0.5,
        )
    captured = capsys.readouterr()
    assert "partial-out" in captured.out and "partial-err" in captured.err
    assert "partial-out" in raised.value.output and "partial-err" in raised.value.stderr
    assert '"timed_out": true' in captured.out


@pytest.mark.parametrize("fmt", ["deb", "rpm"])
def test_runtime_dependencies_are_installed_in_sdk(tmp_path, monkeypatch, fmt):
    from types import SimpleNamespace

    from pg_maker.extensions import container

    calls = []

    def mapped_path(*parts):
        path = Path(*parts)
        if path.is_absolute() and not path.is_relative_to(tmp_path):
            return tmp_path / path.as_posix().lstrip("/")
        return path

    for path in ("usr/sbin", "tmp", "work/source/database-packages"):
        (tmp_path / path).mkdir(parents=True)

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0,
            stdout="builder:x:1000:1000::/home/builder:/bin/sh"
            if argv[:2] == ["getent", "passwd"]
            else "",
        )

    monkeypatch.setattr(container, "Path", mapped_path)
    monkeypatch.setattr(container, "run", fake_run)
    dependency = "runtime-extension-provider"
    job = {
        "uid": 1000,
        "runtime_dependencies": [dependency],
        "build_dependencies": [],
        "config": {
            "target": {
                "format": fmt,
                "provider": "community",
                "pgdg": False,
                "sdk_packages": ["server-devel"],
                "runtime_packages": ["server"],
                "split_bundled": False,
            }
        },
    }
    container.provision(job, build=True)
    installs = [
        argv
        for argv in calls
        if argv[0] in {"apt-get", "dnf"} and "install" in argv and "--download-only" not in argv
    ]
    assert any(dependency in argv for argv in installs)


@pytest.mark.parametrize("kind", ["prepare", "build"])
def test_extension_cli_waits_and_reports_worker_failure(project, tmp_path, kind):
    import sys

    config = load_project(project)
    config["execution"]["min_free_disk_bytes"] = 10**20
    operations = tmp_path / "ops"
    if kind == "prepare":
        document = yaml.safe_load(project.read_text())
        document["spec"]["execution"] = config["execution"]
        project.write_text(yaml.safe_dump(document))
        args = [
            str(project),
            "--out",
            str(tmp_path / "resolved.json"),
            "--input-store",
            str(tmp_path / "inputs"),
            "--offline",
        ]
    else:
        lock = tmp_path / "lock.json"
        document = example_lock(config)
        document["transport"] = {"input_store": str(tmp_path / "inputs")}
        write_json(lock, document)
        args = [str(lock)]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pg_maker",
            "--machine",
            "extension",
            kind,
            *args,
            "--operations",
            str(operations),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 3, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["status"] == "failed"
    assert envelope["error"]["code"] == "precondition_failed"
    assert envelope["result"]["status"] == "failed"
    assert not envelope["result"]["resources"]


@pytest.mark.parametrize("kind", ["prepare", "build"])
def test_extension_request_id_reuses_operation_and_rejects_changed_input(project, tmp_path, kind):
    from pg_maker.api import MakerService

    document = yaml.safe_load(project.read_text())
    document["spec"]["execution"]["min_free_disk_bytes"] = 10**20
    project.write_text(yaml.safe_dump(document))
    service = MakerService(tmp_path / "ops")
    if kind == "prepare":
        args = (project,)
        kwargs = {
            "output": tmp_path / "resolved.json",
            "input_store": tmp_path / "inputs",
            "offline": True,
        }
    else:
        lock = tmp_path / "lock.json"
        locked = example_lock(load_project(project))
        locked["transport"] = {"input_store": str(tmp_path / "inputs")}
        write_json(lock, locked)
        args, kwargs = (lock,), {}
    start = getattr(service, "extension_" + kind)
    first = start(*args, **kwargs, request_id="retry-request")
    service.wait(service.directory / first["operation_id"], timeout_seconds=15)
    second = start(*args, **kwargs, request_id="retry-request")
    assert first["operation_id"] == second["operation_id"]
    assert second["request_id"] == "retry-request"
    assert len(list(service.directory.glob("*/state.json"))) == 1
    if kind == "prepare":
        document["spec"]["maintainer"] = "Changed <changed@example.invalid>"
        project.write_text(yaml.safe_dump(document))
    else:
        locked["spec"]["config"]["maintainer"] = "Changed <changed@example.invalid>"
        updated = seal("lock", locked["spec"])
        updated["transport"] = locked["transport"]
        write_json(lock, updated)
    with pytest.raises(PreconditionError, match="different inputs"):
        start(*args, **kwargs, request_id="retry-request")


def test_extension_cli_forwards_request_id_on_detached_retry(project, tmp_path):
    import sys

    from pg_maker.api import MakerService

    config = yaml.safe_load(project.read_text())
    config["spec"]["execution"]["min_free_disk_bytes"] = 10**20
    project.write_text(yaml.safe_dump(config))
    operations = tmp_path / "operations"
    argv = [
        sys.executable,
        "-m",
        "pg_maker",
        "--machine",
        "--request-id",
        "cli-retry",
        "extension",
        "prepare",
        str(project),
        "--out",
        str(tmp_path / "lock.json"),
        "--input-store",
        str(tmp_path / "inputs"),
        "--operations",
        str(operations),
        "--offline",
        "--detach",
    ]
    first = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    assert first.returncode == 0, first.stderr
    started = json.loads(first.stdout)["result"]
    assert started["request_id"] == "cli-retry"
    MakerService(operations).wait(operations / started["operation_id"], timeout_seconds=15)
    retry = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    assert retry.returncode == 3, retry.stderr
    payload = json.loads(retry.stdout)
    assert payload["request_id"] == "cli-retry"
    assert payload["status"] == "failed"
    assert payload["result"]["operation_id"] == started["operation_id"]
    assert len(list(operations.glob("*/state.json"))) == 1


def test_auto_publication_failure_reaches_ci_and_retry_is_idempotent(project, tmp_path):
    import sys

    from test_publication import directory_config, write_config

    config = load_project(project)
    config["execution"]["min_free_disk_bytes"] = 10**20
    lock = tmp_path / "lock.json"
    document = example_lock(config)
    document["transport"] = {"input_store": str(tmp_path / "inputs")}
    write_json(lock, document)
    publication = write_config(tmp_path / "publication.yaml", directory_config(tmp_path / "dest"))
    operations = tmp_path / "ops"
    args = [
        sys.executable,
        "-m",
        "pg_maker",
        "--machine",
        "--request-id",
        "ci-auto",
        "extension",
        "build",
        str(lock),
        "--operations",
        str(operations),
        "--publish-config",
        str(publication),
    ]
    ids = []
    for _ in range(2):
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        assert result.returncode == 3, result.stderr + result.stdout
        payload = json.loads(result.stdout)
        assert payload["status"] == "failed"
        assert payload["result"]["operation_kind"] == "publication"
        ids.append(payload["result"]["operation_id"])
    assert ids[0] == ids[1]
    assert len(list(operations.glob("*/state.json"))) == 2
    assert not (tmp_path / "dest").exists()
