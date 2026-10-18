"""Resolve YAML and recipes without fetching sources or touching Docker."""

from __future__ import annotations

import copy
import os
import re
import shlex
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from pg_maker.documents import validate_document
from pg_maker.errors import UnsupportedError, ValidationError
from pg_maker.identity import identities
from pg_maker.postgresql import recipe as postgresql_recipe
from pg_maker.postgresql import version_info
from pg_maker.storage import content_hash
from pg_maker.yamlio import load_yaml


def resource_path(*parts: str) -> Path:
    # Wheels are installed unpacked; retaining real paths also supports detached workers.
    return Path(str(files("pg_maker").joinpath(*parts)))


def merge(base: dict, update: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in update.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def compatible(version: str, requirement: str, subject: str) -> None:
    try:
        accepted = SpecifierSet(requirement).contains(Version(version), prereleases=True)
    except (InvalidSpecifier, InvalidVersion) as exc:
        raise ValidationError("invalid PostgreSQL compatibility declaration") from exc
    if not accepted:
        raise UnsupportedError(
            "PostgreSQL version is outside recipe compatibility",
            details={
                "subject": subject,
                "version": version,
                "compatible_postgresql": requirement,
            },
        )


def mark_leaves(value, origin: str, path: str, provenance: dict) -> None:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            mark_leaves(item, origin, path + "/" + key, provenance)
    elif isinstance(value, list) and value:
        for index, item in enumerate(value):
            mark_leaves(item, origin, path + f"/{index}", provenance)
    else:
        provenance[path] = origin


@dataclass(frozen=True)
class EffectiveConfig:
    data: dict
    provenance: dict[str, str]
    bindings: dict[str, str]

    def to_document(self) -> dict:
        return {"data": self.data, "provenance": self.provenance, "bindings": self.bindings}


def platform_recipe(reference: str, base: Path, config_dir: Path | None) -> tuple[dict, Path]:
    explicit = "/" in reference or reference.endswith((".yaml", ".yml"))
    if explicit:
        path = (base / reference).resolve()
    else:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", reference):
            raise ValidationError("invalid platform id")
        builtin = resource_path("recipes", "platforms", reference + ".yaml")
        custom = config_dir / "platforms" / (reference + ".yaml") if config_dir else None
        path = custom if custom and custom.is_file() else builtin
        if path == custom and builtin.is_file():
            if not load_yaml(path, "platform")["spec"].get("replace_builtin", False):
                raise ValidationError("platform override needs replace_builtin or an explicit path")
    if not path.is_file():
        raise UnsupportedError("platform recipe is unavailable", details={"platform": reference})
    recipe = load_yaml(path, "platform")
    if not explicit and recipe["metadata"]["id"] != reference:
        raise ValidationError("platform id differs from its filename")
    return recipe, path


def _unmanaged(arguments: list[str], pointer: str) -> None:
    managed = re.compile(
        r"^(--(?:prefix|exec-prefix|[a-z]*dir|with(?:out)?-(?:llvm|ssl|openssl|icu|lz4|zstd|"
        r"libxml|libxslt|pam|systemd|uuid|perl|python|tcl|ldap|gssapi)|"
        r"(?:enable|disable)-(?:debug|cassert|nls|tap-tests)))(?:=|$)|"
        r"^(?:-[Oog]|-m(?:arch|cpu|tune)|-flto|-D_FORTIFY_SOURCE|(?:C|CPP|CXX|LD)FLAGS=)"
    )
    for value in arguments:
        if not value or any(c in value for c in "\x00\n\r"):
            raise ValidationError("invalid build argument", details={"pointer": pointer})
        try:
            tokens = shlex.split(value)
        except ValueError as exc:
            raise ValidationError("malformed build argument", details={"pointer": pointer}) from exc
        if any(managed.search(token) for token in tokens):
            raise ValidationError(
                "argument overrides a managed build setting",
                details={
                    "pointer": pointer,
                    "argument": value,
                },
            )


def build_flags(defaults: dict, feature: dict, target: dict, recipe: dict, extra: list) -> dict:
    build = copy.deepcopy(defaults["build"])
    additions = feature["spec"]["build_options"]
    build["options"].update(additions.get("options", {}))
    if build["backend"] not in recipe["backends"]:
        raise UnsupportedError("build backend is not supported by platform")
    if build["backend"] != "autotools" or build["flags"]["lto"]:
        raise UnsupportedError("this planner supports autotools without LTO")
    for name, value in build["options"].items():
        if name not in recipe["options"] or value not in recipe["options"][name]:
            raise UnsupportedError("unsupported build option", details={"option": name})
        if name not in recipe["option_configure"] or name not in recipe["option_dependencies"]:
            raise ValidationError("platform option lacks configure or dependency policy")
    mode = defaults["build_modes"][build["mode"]]
    flags = {
        "cflags": list(mode["cflags"]),
        "cxxflags": list(mode["cflags"]),
        "cppflags": [*build["flags"]["cppflags"], *mode["cppflags"]],
        "ldflags": list(build["flags"]["ldflags"]),
    }
    for key in flags:
        _unmanaged(additions.get(key, []), "/feature/build_options/" + key)
        flags[key] += additions.get(key, [])
    cpu = recipe["cpu_targets"][target["architecture"]][target["cpu_target"]]
    flags["cflags"] += cpu
    flags["cxxflags"] += cpu
    for name, value in additions.get("defines", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name == "_FORTIFY_SOURCE":
            raise ValidationError("invalid or managed feature define")
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_.+-]+", str(value)):
            raise ValidationError("define values must be single preprocessor tokens")
        flags["cppflags"].append("-D" + name + ("=" + str(value) if value is not None else ""))
    _unmanaged(extra, "/spec/extra_build_args")
    _unmanaged(additions.get("configure_args", []), "/feature/build_options/configure_args")
    build["resolved_flags"] = flags
    build["configure_args"] = [
        *mode["configure_options"],
        *[
            recipe["option_configure"][name]["enabled" if value else "disabled"]
            for name, value in sorted(build["options"].items())
        ],
        *additions.get("configure_args", []),
        *extra,
    ]
    return build


def load_config(
    manifest_path: Path,
    *,
    config_dir: Path | None = None,
    features: list[str] | None = None,
    build_mode: str | None = None,
    with_doc: bool | None = None,
    doc_languages: list[str] | None = None,
) -> EffectiveConfig:
    from pg_maker.changes.loader import load_changes, load_components, required_tests

    manifest_path = manifest_path.resolve()
    manifest = load_yaml(manifest_path, "build")
    spec = manifest["spec"]
    version = spec["postgresql"]["version"]
    pg_recipe = postgresql_recipe(version)
    if config_dir is not None:
        config_dir = config_dir.resolve()
    elif "config_dir" in spec:
        config_dir = (manifest_path.parent / spec["config_dir"]).resolve()
    elif os.environ.get("PG_MAKER_CONFIG_DIR"):
        config_dir = Path(os.environ["PG_MAKER_CONFIG_DIR"]).resolve()
    if config_dir is not None and not config_dir.is_dir():
        raise ValidationError("selected config directory does not exist")
    provenance, bindings = {}, {"source": str(manifest_path.parent)}
    docs_base = resource_path("bundled", "config")
    settings = {}
    for name in ("vendor", "defaults", "documentation"):
        builtin = resource_path("bundled", "config", name + ".yaml")
        value = load_yaml(builtin, name)
        mark_leaves(value["spec"], "builtin:" + name, "/" + name, provenance)
        path = config_dir / (name + ".yaml") if config_dir else None
        if name == "documentation" and spec.get("documentation", {}).get("profile"):
            path = manifest_path.parent / spec["documentation"]["profile"]
            if not path.is_file():
                raise ValidationError("documentation profile does not exist")
        if path is not None and path.is_file():
            override = load_yaml(path)
            value = merge(value, override)
            validate_document(value, name)
            mark_leaves(override.get("spec", {}), str(path), "/" + name, provenance)
            if name == "documentation":
                docs_base = path.parent
        settings[name] = value["spec"]
    defaults = settings["defaults"]
    for section in ("build", "validation", "execution"):
        defaults[section] = merge(defaults[section], spec.get(section, {}))
        mark_leaves(spec.get(section, {}), str(manifest_path), "/defaults/" + section, provenance)
    if build_mode is not None:
        if build_mode not in {"release", "debug"}:
            raise ValidationError("unknown build mode")
        defaults["build"]["mode"] = build_mode
        provenance["/defaults/build/mode"] = "cli:build-mode"
    docs = settings["documentation"]
    docs.update(
        {key: val for key, val in spec.get("documentation", {}).items() if key != "profile"}
    )
    if with_doc is not None:
        docs["enabled"] = with_doc
        provenance["/documentation/enabled"] = "cli:with-doc"
    if doc_languages is not None:
        docs["languages"] = doc_languages
        for i in range(len(doc_languages)):
            provenance[f"/documentation/languages/{i}"] = "cli:doc-language"
    if docs["enabled"]:
        from pg_maker.documentation.config import normalize
        from pg_maker.postgresql import documentation_fixes

        docs["rules"] = [*documentation_fixes(version, pg_recipe), *docs["rules"]]
        normalize(docs, docs_base, bindings, version=version, validation=defaults["validation"])
    elif doc_languages is not None:
        raise ValidationError("doc-language requires enabled documentation")
    selected = features if features is not None else spec["features"]
    if not selected or len(selected) != len(set(selected)):
        raise ValidationError("feature selection must be nonempty and unique")
    feature_docs, used_ids, numeric_ids = [], set(), set()
    for reference in selected:
        explicit = "/" in reference or reference.endswith((".yaml", ".yml"))
        if explicit:
            path = (manifest_path.parent / reference).resolve()
        else:
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", reference):
                raise ValidationError("invalid feature id")
            path = (
                (config_dir or resource_path("bundled", "config"))
                / "features"
                / (reference + ".yaml")
            )
        feature = load_yaml(path, "feature")
        fid, fs = feature["metadata"]["id"], feature["spec"]
        if not explicit and fid != reference:
            raise ValidationError("feature id differs from its filename")
        if fid in used_ids or (fs["numeric_id"] is not None and fs["numeric_id"] in numeric_ids):
            raise ValidationError("duplicate feature id or numeric_id")
        used_ids.add(fid)
        if fs["numeric_id"] is not None:
            numeric_ids.add(fs["numeric_id"])
        compatible(version, fs.get("compatible_postgresql", ">=14"), fid)
        changes, file_bindings = load_changes(fs["changes"], path.parent, version)
        components, component_bindings = load_components(fs["components"], path.parent, version)
        bindings.update({fid + "/" + key: val for key, val in file_bindings.items()})
        bindings.update({fid + "/" + key: val for key, val in component_bindings.items()})
        feature["spec"]["changes"], feature["spec"]["components"] = changes, components
        if docs["enabled"]:
            from pg_maker.documentation.config import load_rules

            fs["documentation"]["rules"] = load_rules(
                fs["documentation"]["rules"], path.parent, fid, bindings
            )
            for component in components:
                if "documentation" in component["spec"]:
                    cid = component["metadata"]["id"]
                    component["spec"]["documentation"]["rules"] = load_rules(
                        component["spec"]["documentation"]["rules"],
                        Path(component_bindings["components/" + cid]),
                        fid + "/" + cid,
                        bindings,
                    )
        required_tests(feature)
        mark_leaves(feature, str(path), f"/features/{len(feature_docs)}", provenance)
        feature_docs.append(feature)
    targets, target_ids = [], set()
    for target in spec["targets"]:
        if target["id"] in target_ids:
            raise ValidationError("duplicate target id")
        target_ids.add(target["id"])
        recipe, path = platform_recipe(target["platform"], manifest_path.parent, config_dir)
        rs = recipe["spec"]
        if docs["enabled"]:
            if "documentation" not in rs or "docs" not in rs["roles"]:
                raise UnsupportedError("platform lacks documentation recipe")
            if set(docs["languages"]) - set(rs["documentation"]["languages"]):
                raise UnsupportedError("platform lacks selected documentation language/fonts")
        compatible(version, rs["compatible_postgresql"], recipe["metadata"]["id"])
        arch = target["architecture"]
        if arch not in rs["architectures"] or target["cpu_target"] not in rs["cpu_targets"].get(
            arch, {}
        ):
            raise UnsupportedError("architecture or CPU target is unsupported")
        if (
            arch not in rs["architecture_names"]
            or not {"server", "client", "libpq"} <= rs["roles"].keys()
        ):
            raise ValidationError("platform lacks architecture or package roles")
        if not target["artifacts"] or len(set(target["artifacts"])) != len(target["artifacts"]):
            raise ValidationError("artifacts must be nonempty and unique")
        if set(target["artifacts"]) - set(rs["artifacts"]) or target["artifacts"] != ["deb"]:
            raise UnsupportedError("this planner supports the DEB artifact family")
        if rs["package_format"] != "deb" or rs["package_manager"] != "apt":
            raise UnsupportedError("this planner supports the apt/DEB recipe adapter")
        if defaults["validation"]["profile"] not in rs["test_profiles"]:
            raise UnsupportedError("test profile is unsupported by platform")
        mark_leaves(target, str(manifest_path), f"/targets/{len(targets)}", provenance)
        mark_leaves(recipe, str(path), f"/targets/{len(targets)}/platform", provenance)
        targets.append({**target, "platform": recipe})
    variants, collisions, filenames = [], {}, set()
    for feature in feature_docs:
        fs, fid = feature["spec"], feature["metadata"]["id"]
        sockets = {
            change["spec"].get("postconditions", {}).get("socket_directory")
            for change in fs["changes"]
        }
        sockets.discard(None)
        if defaults["common_changes"]:
            sockets.add("/var/run/postgresql")
        if len(sockets) > 1:
            raise ValidationError("conflicting socket_directory postconditions")
        socket = next(iter(sockets), "/tmp")
        if not socket.startswith("/") or ".." in Path(socket).parts:
            raise ValidationError("socket_directory must be absolute and normalized")
        baseline = defaults["validation"].get("baseline", "auto")
        expectations = any(
            "baseline" in t["expectations"] for c in fs["changes"] for t in c["spec"]["acceptance"]
        )
        if baseline is False and expectations:
            raise ValidationError("baseline is required by acceptance expectations")
        baseline = bool(fs["changes"]) if baseline == "auto" else baseline is True
        for target in targets:
            rs = target["platform"]["spec"]
            build = build_flags(defaults, feature, target, rs, spec.get("extra_build_args", []))
            if build["backend"] not in pg_recipe["execution_backends"]:
                raise UnsupportedError("PostgreSQL recipe does not implement this backend")
            unavailable = set(build["options"]) & set(pg_recipe["unavailable_options"])
            if unavailable:
                raise UnsupportedError(
                    "build option is unavailable in this PostgreSQL major",
                    details={"options": sorted(unavailable), "version": version},
                )
            identity = identities(
                settings["vendor"],
                defaults,
                feature,
                target,
                {**rs, "id": target["platform"]["metadata"]["id"]},
                version,
                spec["product"],
                build["mode"],
                docs["enabled"],
            )
            for package in identity["packages"]:
                if package["filename"] in filenames:
                    raise ValidationError("package filename collision across targets")
                filenames.add(package["filename"])
            # Alternative OS/architecture targets may intentionally use the same runtime path.
            for name in [
                identity["service"],
                *identity["paths"].values(),
                *[p["name"] for p in identity["packages"]],
            ]:
                key = (target["id"], name)
                if key in collisions and collisions[key] != fid:
                    raise ValidationError("feature identity collision", details={"value": name})
                collisions[key] = fid
            variants.append(
                {
                    "id": fid + "." + target["id"],
                    "feature": fid,
                    "target": target["id"],
                    "build": build,
                    "identity": identity,
                    "socket_directory": socket,
                    "baseline": baseline,
                }
            )
    if spec.get("coinstall") and len(feature_docs) > 1:
        raise ValidationError("coinstall features require distinct service ports")
    previous_releases = []
    previous_variants = set()
    for release in spec.get("previous_releases", []):
        identifier = release["variant"]
        if identifier in previous_variants or identifier not in {v["id"] for v in variants}:
            raise ValidationError("previous release must reference one unique selected variant")
        previous_variants.add(identifier)
        if {p["role"] for p in release["packages"]} != {"server", "client", "libpq"}:
            raise ValidationError("previous release requires server, client and libpq packages")
        if not re.fullmatch(r"[0-9]+\.[0-9]+", release["postgresql_version"]):
            raise ValidationError("previous PostgreSQL version must be M.m")
        if version_info(release["postgresql_version"])["major"] != version_info(version)["major"]:
            raise UnsupportedError("major-version upgrade requires a separate future adapter")
        packages = []
        for package in release["packages"]:
            key = "previous/" + identifier + "/" + package["role"]
            bindings[key] = str((manifest_path.parent / package["path"]).resolve())
            packages.append({"role": package["role"], "input": key, "sha256": package["sha256"]})
        previous_releases.append({**release, "packages": packages})
    data = {
        "name": manifest["metadata"]["name"],
        "source": spec["source"],
        "postgresql": spec["postgresql"],
        "postgresql_recipe": pg_recipe,
        "product": spec["product"],
        **settings,
        "features": feature_docs,
        "targets": targets,
        "variants": variants,
        "previous_releases": previous_releases,
    }
    # Every resolved leaf has an origin; derived fields explicitly identify their producer.
    all_leaves = {}
    for key in ("source", "postgresql", "product"):
        mark_leaves(data[key], str(manifest_path), "/" + key, provenance)
    mark_leaves(data, "derived:config/v1", "", all_leaves)
    provenance = {key: provenance.get(key, origin) for key, origin in all_leaves.items()}
    return EffectiveConfig(data, provenance, bindings)


def environment_requirements(config: dict) -> dict:
    result = []
    for target in config["targets"]:
        recipe = target["platform"]
        rs = recipe["spec"]
        variants = [v for v in config["variants"] if v["target"] == target["id"]]
        components = [c for f in config["features"] for c in f["spec"]["components"]]
        for role in ("build", "test"):
            definition = rs["environments"][role]
            packages = set(definition["packages"])
            tools = dict(definition["tools"])
            minimums = {}
            if role == "build":
                pg_recipe = config["postgresql_recipe"]
                minimums = {key: value for key, value in pg_recipe["tools"].items() if value}
                minimums["make"] = str(
                    max(Version(minimums["make"]), Version(pg_recipe["maker_make_minimum"]))
                )
                if any(v["build"]["options"].get("llvm") for v in variants):
                    tools.setdefault("llvm", ["llvm-config", "--version"])
                    tools.setdefault("clang", ["clang", "--version"])
                    minimums.update(llvm=pg_recipe["llvm_minimum"], clang=pg_recipe["llvm_minimum"])
            docs = config["documentation"]
            if docs["enabled"]:
                recipe_docs = rs["documentation"]
                if role == "build":
                    packages.update(recipe_docs["packages"])
                    tools.update(recipe_docs["tools"])
                    for language in docs["languages"]:
                        packages.update(recipe_docs["languages"][language])
                    for fmt in docs["formats"]:
                        packages.update(recipe_docs["format_packages"].get(fmt, []))
                    packages.update(recipe_docs["backend_packages"].get(docs["html_backend"], []))
                else:
                    packages.update(["python3-lxml", "poppler-utils", "groff-base"])
            if role == "build":
                for variant in variants:
                    for option, enabled in variant["build"]["options"].items():
                        if enabled:
                            packages.update(rs["option_dependencies"][option])
                for component in components:
                    packages.update(component["spec"]["build_dependencies"])
            runtime_packages = set()
            if role == "build":
                for dependencies in rs["runtime_dependencies"].values():
                    runtime_packages.update(dependencies)
                for dependencies in rs.get("service_dependencies", {}).values():
                    runtime_packages.update(dependencies)
                for variant in variants:
                    for option, enabled in variant["build"]["options"].items():
                        if enabled:
                            runtime_packages.update(
                                rs.get("option_runtime_dependencies", {}).get(option, [])
                            )
                for component in components:
                    runtime_packages.update(component["spec"]["runtime_dependencies"])
            if any(not re.fullmatch(r"[a-z0-9][a-z0-9+.:=-]*", p) for p in packages):
                raise ValidationError("invalid package dependency")
            result.append(
                {
                    "id": target["id"] + "." + role,
                    "target": target["id"],
                    "role": role,
                    "recipe_hash": content_hash(recipe),
                    "os": rs["os"],
                    "architecture": target["architecture"],
                    "image": definition["image"],
                    "packages": sorted(packages),
                    "runtime_packages": sorted(runtime_packages),
                    "tools": tools,
                    "tool_minimums": minimums,
                    "locales": config["defaults"]["validation"]["database_locales"],
                }
            )
    return {
        "adapter": "pg_maker/apt-environment/v3",
        "environments": result,
        "builder_user": config["defaults"]["execution"]["build_user"],
    }
