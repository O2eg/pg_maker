"""Extension identities, independent staging, packaging and isolated qualification."""

from __future__ import annotations

import copy
import shutil
import time
from pathlib import Path

from pg_maker.config import resource_path
from pg_maker.errors import PreconditionError
from pg_maker.extensions.execution import Session, setup_workspace
from pg_maker.packaging.deb import inspect_deb
from pg_maker.sources.store import InputStore, inventory, materialize
from pg_maker.storage import content_hash, file_hash, read_json, write_json
from pg_maker.validation.diagnostics import classify


def verify_retained_sources(source, baseline):
    """Generated objects may remain; every original source entry must still match the lock."""
    for entry in inventory(baseline)["entries"]:
        path = source / entry["path"]
        if entry["type"] == "symlink":
            valid = path.is_symlink() and path.readlink().as_posix() == entry["target"]
        elif entry["type"] == "directory":
            valid = path.is_dir() and not path.is_symlink()
        else:
            valid = path.is_file() and not path.is_symlink() and file_hash(path) == entry["sha256"]
        if valid and entry["type"] != "symlink":
            valid = path.stat().st_mode & 0o7777 == entry["mode"]
        if not valid:
            raise PreconditionError(
                "retained source differs from lock; prepare the edited recipe again",
                details={"path": entry["path"]},
            )


def identity(extension, target, sdk):
    name = extension["spec"]["sql_extension"].replace("_", "-")
    meta = extension["packaging"]
    suffix = "-yc-clone" if target["provider"] == "profile" else ""
    deb = target["format"] == "deb"
    package_name = meta.get("package_name") or f"postgresql-{target['major']}-{name}{suffix}"
    upstream = meta["version"]
    release = str(meta["package_revision"]) + (".deb" if deb else ".el") + target["os"]["version"]
    if target["dependency_policy"] == "exact":
        # Different exact SDK dependencies must not share one native package version.
        release += "+sdk." + content_hash(sdk["server_package"])[7:27]
    version = upstream + "-" + release
    arch = (
        target["architecture"]
        if deb
        else {"amd64": "x86_64", "arm64": "aarch64"}[target["architecture"]]
    )
    server = sdk["server_package"]
    if target["dependency_policy"] == "exact":
        dependencies = [
            server["name"]
            + (" (= " + server["version"] + ")" if deb else " = " + server["version"])
        ]
    else:
        minimum = sdk["postgresql_version"]
        dependencies = [
            server["name"] + (" (>= " + minimum + ")" if deb else " >= " + minimum),
            server["name"]
            + (
                " (<< " + str(target["major"] + 1) + "~)"
                if deb
                else " < " + str(target["major"] + 1)
            ),
        ]
    filename = (
        f"{package_name}_{version}_{arch}.deb" if deb else f"{package_name}-{version}.{arch}.rpm"
    )
    return {
        "name": package_name,
        "version": version,
        "upstream_version": upstream,
        "release": release,
        "filename": filename,
        "architecture": arch,
        "dependencies": dependencies,
    }


def build_extensions(request, context):
    locked = request.lock["spec"]
    config = copy.deepcopy(locked["config"])
    root = setup_workspace(context)
    store = InputStore(Path(request.input_store))
    target = locked["target"]
    timings = []
    for extension in config["extensions"]:
        extension["package"] = identity(extension, config["target"], target)
    extension_packages = {
        name: e["package"]
        for e in config["extensions"]
        for name in e["spec"]["sql_extension_versions"]
    }
    if len({e["package"]["name"] for e in config["extensions"]}) != len(config["extensions"]):
        raise PreconditionError("extension package naming collision")
    job = {
        "config": config,
        "target": target,
        "extension_packages": extension_packages,
        "migration": locked.get("migration", []),
    }
    records = []
    with Session(context, locked["images"]["build"], config) as session:
        session.helper("inspect", job)
        if read_json(root / "output/target.json") != target:
            raise PreconditionError("installed target SDK differs from locked identity")
        for extension in config["extensions"]:
            name = extension["spec"]["sql_extension"]
            source = root / "build/components" / name
            receipt = root / "build/receipts" / (name + ".json")
            key = content_hash(
                {
                    "recipe": extension,
                    "target": target,
                    "image": locked["images"]["build"],
                    "implementation": locked["implementation"],
                }
            )
            previous = read_json(receipt) if receipt.is_file() else {}
            stage = root / "build/staging" / name
            reused = (
                previous.get("key") == key
                and stage.is_dir()
                and previous.get("tree_hash") == content_hash(inventory(stage))
            )
            started = time.monotonic()
            baseline = store.tree(extension["spec"]["source"]["source_hash"])
            if source.exists():
                verify_retained_sources(source, baseline)
            if not reused:
                if not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    materialize(baseline, source)
                    verify_retained_sources(source, baseline)
                session.helper("build", {**job, "extension": extension})
                write_json(receipt, {"key": key, "tree_hash": content_hash(inventory(stage))})
            timings.append(
                {
                    "stage": "build",
                    "extension": name,
                    "reused": reused,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            started = time.monotonic()
            # Remove an interrupted control directory before packaging the retained tree.
            if (stage / "DEBIAN").is_dir():
                shutil.rmtree(stage / "DEBIAN")
            session.helper("package", {**job, "extension": extension})
            record = read_json(root / "output/packages" / (name + ".json"))
            package = root / "output/packages" / record["path"]
            if file_hash(package) != record["sha256"]:
                raise PreconditionError("new extension package changed")
            if config["target"]["format"] == "deb":
                inspected = inspect_deb(package)
                actual = {e["path"]: e for e in inspected["entries"] if e["type"] != "directory"}
                expected = {e["path"]: e for e in record["files"]}
                if actual != expected:
                    raise PreconditionError("DEB payload differs from extension staging")
                if (
                    inspected["control"]["Package"] != record["name"]
                    or inspected["control"]["Version"] != record["version"]
                ):
                    raise PreconditionError("DEB identity differs from extension recipe")
            records.append(record)
            owned = {}
            for package_record in records:
                for file in package_record["files"]:
                    if file["path"] in owned:
                        raise PreconditionError(
                            "extension packages install overlapping files",
                            details={
                                "path": file["path"],
                                "owners": [owned[file["path"]], package_record["name"]],
                            },
                        )
                    owned[file["path"]] = package_record["name"]
            timings.append(
                {
                    "stage": "package",
                    "extension": name,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            context.advance("extension_packaged", step_id=name)
        if config["validation"]["run_tests"]:
            started = time.monotonic()
            session.helper("test-source", job, test=True)
            for log in (root / "output/source-test-logs").rglob("*"):
                if (
                    log.is_file()
                    and not classify(log, None, test_command=True, generated_log=True)["passed"]
                ):
                    raise PreconditionError("blocking diagnostic in extension source test log")
            timings.append(
                {"stage": "test-source", "elapsed_ms": int((time.monotonic() - started) * 1000)}
            )
            test_root = root / "package-test-workspace"
            (test_root / "source").mkdir(parents=True, exist_ok=True)
            repository = test_root / "source/repository"
            if repository.exists():
                shutil.rmtree(repository)
            session.runner._control(
                [
                    "cp",
                    session.handle.container_id + ":/opt/pg-maker/extension-runtime-repository",
                    str(repository),
                ],
                timeout=300,
            )
            expected = {r["path"]: r["sha256"] for r in locked["runtime_repository"]}
            actual = {
                p.relative_to(repository).as_posix(): file_hash(p)
                for p in repository.rglob("*")
                if p.is_file() and p.name != "lock"
            }
            if actual != expected or any(p.is_symlink() for p in repository.rglob("*")):
                raise PreconditionError("offline runtime repository differs from locked bytes")
    package_results = {"passed": False, "status": "skipped"}
    if config["validation"]["run_tests"]:
        shutil.copyfile(
            resource_path("extensions", "container.py"), test_root / "source/extension-container.py"
        )
        shutil.copytree(root / "output/packages", test_root / "source/packages", dirs_exist_ok=True)
        started = time.monotonic()
        try:
            with Session(context, locked["images"]["test"], config, workspace=test_root) as session:
                session.helper("test-packages", {**job, "packages": records}, test=True)
        finally:
            if (test_root / "output").is_dir():
                shutil.copytree(
                    test_root / "output", root / "output/package-tests", dirs_exist_ok=True
                )
        package_results = read_json(test_root / "output/package-tests.json")
        if not package_results["passed"]:
            raise PreconditionError("extension package tests failed")
        timings.append(
            {"stage": "test-packages", "elapsed_ms": int((time.monotonic() - started) * 1000)}
        )
    migration = locked.get("migration", [])
    for record in migration:
        destination = root / "output/target-packages" / record["filename"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(store.blob(record["sha256"]), destination)
    if migration:
        write_json(root / "output/target-packages/migration.json", migration)
    report = {
        "scope": "standalone-extensions",
        "lock_hash": request.lock["lock_hash"],
        "target": target,
        "packages": records,
        "timings": timings,
        "qualification": "extension-package-verified"
        if package_results["passed"]
        else "unverified",
        "source_tests": "passed" if package_results["passed"] else "skipped",
        "package_tests": package_results,
        "database_core_tests": "not-in-scope",
    }
    write_json(root / "output/report.json", report)
    write_json(root / "output/lock.json", request.lock)
    checksums = "".join(r["sha256"][7:] + "  " + r["path"] + "\n" for r in records)
    (root / "output/packages/SHA256SUMS").write_text(checksums)
    if config["execution"]["keep_workspace"] == "on-failure":
        # Session release has already returned scratch ownership to the host user.
        shutil.rmtree(root / "build")
        if (root / "package-test-workspace").exists():
            shutil.rmtree(root / "package-test-workspace")
    evidence = [
        {"path": p.relative_to(root / "output").as_posix(), "sha256": file_hash(p)}
        for p in sorted((root / "output").rglob("*"))
        if p.is_file() and p.name != "extension-manifest.json"
    ]
    manifest = {
        "api_version": "pg_maker/extension-artifacts/v1",
        "lock_hash": request.lock["lock_hash"],
        "qualification": report["qualification"],
        "evidence": evidence,
    }
    manifest["manifest_hash"] = content_hash(manifest)
    write_json(root / "output/extension-manifest.json", manifest)
    return report
