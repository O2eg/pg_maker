"""Extension projects, target contracts and pure dependency planning."""

from __future__ import annotations

import copy
import os
import re
import shutil
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

import yaml

from pg_maker.config import resource_path
from pg_maker.documents import validate_document
from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.storage import content_hash, file_hash, read_json, utc_now
from pg_maker.yamlio import load_yaml

BUILTINS = ("pg_wait_sampling", "pg_stat_kcache", "pg_repack")
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}


def initialize(
    directory,
    *,
    extensions,
    major=18,
    profile=None,
    packages=None,
    platform="debian-13",
    architecture="amd64",
    split_bundled=False,
):
    """Copy editable recipes; importing a profile never changes that profile."""
    if directory.exists() and any(directory.iterdir()):
        raise PreconditionError("extension project directory is not empty")
    if not extensions or set(extensions) - set(BUILTINS):
        raise ValidationError("choose built-in extensions or supply an editable recipe")
    if architecture not in ARCHITECTURES:
        raise ValidationError("unsupported extension architecture")
    if platform not in {"debian-13", "rocky-9", "rocky-10"}:
        raise UnsupportedError("unsupported extension platform")
    os_id, os_version = platform.split("-")
    fmt = "deb" if os_id == "debian" else "rpm"
    target = {
        "provider": "community",
        "major": major,
        "os": {"id": os_id, "version": os_version},
        "architecture": architecture,
        "format": fmt,
        "base_image": "debian:13-slim" if fmt == "deb" else "rockylinux/rockylinux:" + os_version,
        "pg_config": f"/usr/lib/postgresql/{major}/bin/pg_config"
        if fmt == "deb"
        else f"/usr/pgsql-{major}/bin/pg_config",
        "server_package": f"postgresql-{major}" if fmt == "deb" else f"postgresql{major}-server",
        "sdk_packages": [f"postgresql-server-dev-{major}"]
        if fmt == "deb"
        else [f"postgresql{major}-devel", "clang", "llvm"],
        "runtime_packages": [f"postgresql-{major}"]
        if fmt == "deb"
        else [f"postgresql{major}-server", f"postgresql{major}-contrib"],
        "packages": [],
        "pgdg": True,
        "dependency_policy": "major",
        "split_bundled": split_bundled,
    }
    if profile:
        from pg_maker.assets import BUILD_PROFILES

        if profile not in BUILD_PROFILES:
            raise ValidationError("unknown database profile")
        document = load_yaml(resource_path("bundled", "profiles", profile + ".yaml"))
        version = document["spec"]["postgresql"]["version"]
        major = int(version.split(".")[0])
        if fmt != "deb":
            raise UnsupportedError("bundled YC database profiles currently provide DEBs only")
        if packages is None:
            raise ValidationError("a database profile requires --packages with its actual DEBs")
        target.update(
            provider="profile",
            profile=profile,
            major=major,
            version=version,
            pg_config=f"/opt/postgresql-yc-clone/{major}/bin/pg_config",
            server_package=f"postgresql-{major}-yc-clone",
            sdk_packages=[],
            runtime_packages=[],
            pgdg=False,
            dependency_policy="exact",
        )
    if packages is not None:
        package_files = sorted(packages.glob("*." + fmt))
        if not package_files:
            raise ValidationError("database package directory is empty")
        target["packages"] = [
            {"path": str(p.absolute()), "sha256": file_hash(p)} for p in package_files
        ]
    directory.mkdir(parents=True, exist_ok=True)
    recipe_dir = directory / "recipes"
    recipe_dir.mkdir()
    entries = []
    for name in dict.fromkeys(extensions):
        if name == "pg_wait_sampling":
            recipe_path = resource_path("bundled", "extensions", name + ".yaml")
        else:
            folder = "managed" if major == 16 else "managed" + str(major)
            recipe_path = resource_path("bundled", "components", folder, name + ".yaml")
        if not recipe_path.is_file():
            raise UnsupportedError("no built-in recipe for this major; provide your own recipe")
        recipe = load_yaml(recipe_path, "component")
        if name == "pg_repack":
            # PGXS links frontend programs with pg_config --libs; server-dev alone
            # does not guarantee the corresponding unversioned linker libraries.
            recipe["spec"]["build_dependencies"] = sorted(
                set(recipe["spec"]["build_dependencies"])
                | set(
                    [
                        "libssl-dev",
                        "libreadline-dev",
                        "zlib1g-dev",
                        "liblz4-dev",
                        "libzstd-dev",
                        "libxml2-dev",
                        "libnuma-dev",
                    ]
                    if fmt == "deb"
                    else [
                        "openssl-devel",
                        "readline-devel",
                        "zlib-devel",
                        "lz4-devel",
                        "libzstd-devel",
                        "libxml2-devel",
                        "numactl-devel",
                        "liburing-devel",
                    ]
                )
            )
        for index, patch in enumerate(recipe["spec"].get("source_patches", [])):
            original = (recipe_path.parent / patch).resolve()
            copied = recipe_dir / (name + "-" + original.name)
            shutil.copyfile(original, copied)
            recipe["spec"]["source_patches"][index] = copied.name
        (recipe_dir / (name + ".yaml")).write_text(yaml.safe_dump(recipe, sort_keys=False))
        entries.append(
            {
                "recipe": "recipes/" + name + ".yaml",
                "version": "1.1.11"
                if name == "pg_wait_sampling"
                else recipe["spec"]["sql_extension_versions"][name],
                "package_revision": 1,
                "license": "BSD-3-Clause" if name == "pg_repack" else "PostgreSQL",
            }
        )
    manifest = {
        "api_version": "pg_maker/extension-build/v1",
        "kind": "ExtensionBuild",
        "spec": {
            "target": target,
            "extensions": entries,
            "execution": {
                "jobs": min(os.cpu_count() or 2, 8),
                "memory_bytes": 4294967296,
                "min_free_disk_bytes": 5368709120,
                "stage_timeout_seconds": 3600,
                "keep_workspace": "on-failure",
            },
            "validation": {"run_tests": True},
            "maintainer": "pg-maker builder <builder@example.invalid>",
        },
    }
    validate_document(manifest, "extension-build")
    path = directory / "extensions.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return {"manifest": str(path), "extensions": list(extensions), "target": target}


def load_project(path):
    document = load_yaml(path, "extension-build")
    config = copy.deepcopy(document["spec"])
    target = config["target"]
    if (target["format"] == "deb") != (target["os"]["id"] == "debian"):
        raise ValidationError("package format does not match target OS")
    if not target["pg_config"].startswith("/") or ".." in Path(target["pg_config"]).parts:
        raise ValidationError("pg_config must be an absolute normalized target path")
    if target["provider"] == "profile" and not target["packages"]:
        raise ValidationError("profile targets require actual database packages")
    recipes, names = [], set()
    for entry in config["extensions"]:
        if not re.fullmatch(r"[0-9][A-Za-z0-9.+~]*", entry["version"]):
            raise ValidationError("invalid extension package version")
        if not re.fullmatch(r"[A-Za-z0-9.+() -]+", entry["license"]):
            raise ValidationError("invalid extension license identifier")
        if entry.get("package_name") and not re.fullmatch(
            r"[a-z0-9][a-z0-9+.-]+", entry["package_name"]
        ):
            raise ValidationError("invalid extension package name")
        recipe_path = (path.parent / entry["recipe"]).resolve()
        recipe = load_yaml(recipe_path, "component")
        spec = recipe["spec"]
        if spec["type"] != "pgxs":
            raise UnsupportedError("standalone recipes currently require the PGXS adapter")
        supported = {
            "type",
            "source",
            "compatible_postgresql",
            "requires",
            "conflicts",
            "build_dependencies",
            "runtime_dependencies",
            "test_dependencies",
            "make_targets",
            "test_targets",
            "install_targets",
            "required_tests",
            "sql_extension",
            "sql_extension_versions",
            "runtime_files",
            "source_patches",
            "preload",
            "test_setup",
            "test_commands",
            "test_settings",
        }
        if any(value for key, value in spec.items() if key not in supported):
            raise UnsupportedError("standalone PGXS recipe contains unsupported build fields")
        if not spec.get("source") or not spec.get("sql_extension_versions"):
            raise ValidationError("standalone recipe needs source and explicit SQL versions")
        if spec.get("required_tests"):
            raise UnsupportedError("standalone requirement-ID test adapters are not implemented")
        name = spec.get("sql_extension")
        if not name or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValidationError("standalone recipe needs a SQL extension name")
        if name in names:
            raise ValidationError("duplicate SQL extension")
        if name not in spec["sql_extension_versions"]:
            raise ValidationError("primary SQL extension version is missing")
        declared = set(spec["sql_extension_versions"])
        if names & declared:
            raise ValidationError("duplicate SQL extension provider")
        names.update(declared)
        recipe["packaging"] = {k: v for k, v in entry.items() if k != "recipe"}
        recipe["base"] = str(recipe_path.parent)
        recipes.append(recipe)
    config["extensions"] = recipes
    if len({r["metadata"]["id"] for r in recipes}) != len(recipes):
        raise ValidationError("duplicate extension component id")
    for package in target["packages"]:
        package["path"] = str((path.parent / package["path"]).resolve())
        if file_hash(Path(package["path"])) != package["sha256"]:
            raise PreconditionError("database package changed")
    return config


def order_extensions(config, sdk):
    """Resolve recipe dependencies against selected modules and installed SQL providers."""
    components = {e["metadata"]["id"]: e for e in config["extensions"]}
    aliases = {}
    for name, extension in components.items():
        for alias in [name, *extension["spec"]["sql_extension_versions"]]:
            aliases[alias] = aliases[alias.replace("_", "-")] = name
    installed = set(sdk["sql_extensions"])
    graph = {}
    for name, extension in components.items():
        dependencies = []
        for required in extension["spec"]["requires"]:
            if required in aliases:
                dependencies.append(aliases[required])
            elif required.replace("-", "_") not in installed:
                raise PreconditionError(
                    "unresolved extension recipe dependency",
                    details={"extension": name, "dependency": required},
                )
        for conflict in extension["spec"]["conflicts"]:
            if conflict in aliases or conflict.replace("-", "_") in installed:
                raise PreconditionError(
                    "conflicting extension provider",
                    details={"extension": name, "conflict": conflict},
                )
        graph[name] = dependencies
    try:
        return [components[name] for name in TopologicalSorter(graph).static_order()]
    except CycleError as exc:
        raise ValidationError("extension dependency cycle") from exc


def seal(kind, spec):
    core = {
        "api_version": "pg_maker/extension-" + kind + "/v1",
        "kind": "Extension" + kind.title(),
        "spec": spec,
    }
    return {**core, kind + "_hash": content_hash(core), "created_at": utc_now()}


def verify(document, kind):
    validate_document(document, "extension-" + kind)
    if document.get("api_version") != "pg_maker/extension-" + kind + "/v1":
        raise ValidationError("unexpected extension document")
    core = {k: document[k] for k in ("api_version", "kind", "spec")}
    if content_hash(core) != document.get(kind + "_hash"):
        raise PreconditionError("extension " + kind + " hash changed")


def plan(lock):
    verify(lock, "lock")
    spec = lock["spec"]
    from pg_maker.planning.documents import implementation_identity

    if spec["implementation"] != implementation_identity():
        raise PreconditionError("pg-maker changed; prepare the extension inputs again")
    steps = [{"id": "target", "kind": "verify-target"}]
    for extension in spec["config"]["extensions"]:
        name = extension["spec"]["sql_extension"]
        steps.extend(
            {"id": name + "-" + kind, "kind": kind, "extension": name}
            for kind in ("build", "package")
        )
    if spec["config"]["validation"]["run_tests"]:
        steps.append({"id": "test-source", "kind": "test-source"})
        steps.append({"id": "test-packages", "kind": "test-packages"})
    steps.append({"id": "export", "kind": "manifest"})
    return seal("plan", {"lock_hash": lock["lock_hash"], "steps": steps})


def load_lock(path):
    locked = read_json(path)
    verify(locked, "lock")
    return locked
