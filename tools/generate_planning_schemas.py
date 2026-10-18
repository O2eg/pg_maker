"""Regenerate the shipped stage-3 JSON schemas (no network references)."""

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src/pg_maker/schemas"
URI = "urn:pg_maker:schema:planning-common:v1"
S = {"type": "string"}
N = {"type": "integer", "minimum": 0}
P = {"type": "integer", "minimum": 1}
B = {"type": "boolean"}
ID = {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,63}$"}
HASH = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}


def obj(fields, required=None):
    return {
        "type": "object",
        "properties": fields,
        "required": list(fields) if required is None else required,
        "additionalProperties": False,
    }


def arr(item=S):
    return {"type": "array", "items": item}


def mapping(item=S):
    return {"type": "object", "additionalProperties": item}


def ref(name):
    return {"$ref": URI + "#/$defs/" + name}


def document(version, kind, spec, revision=False):
    meta = {"id": ID, **({"revision": P} if revision else {})}
    return obj(
        {
            "api_version": {"const": version},
            "kind": {"const": kind},
            "metadata": obj(meta),
            "spec": spec,
        }
    )


file_input = obj({"input": S, "sha256": HASH})
doc_file = {"oneOf": [S, file_input]}
selector = obj({"id": S, "tag": S, "parent": obj({"id": S, "tag": S}, [])}, [])
doc_rule = obj(
    {
        "id": S,
        "features": arr(),
        "action": {
            "enum": [
                "insert-before",
                "insert-after",
                "append",
                "prepend",
                "replace",
                "remove",
                "set-text",
                "set-attribute",
                "exact-text",
                "copy-file",
                "copy-tree",
                "replace-file",
                "add-page",
                "release-notes",
            ]
        },
        "file": S,
        "selector": selector,
        "expect_matches": N,
        "fragment": doc_file,
        "source": doc_file,
        "text": S,
        "attribute": S,
        "old": S,
        "sha256": HASH,
        "entity": S,
        "include_file": S,
        "language": S,
        "translate": B,
    },
    ["id", "action", "file", "expect_matches"],
)
doc_rules = document(
    "pg_maker/doc-rules/v1", "DocumentationRules", obj({"rules": arr(doc_rule)}), True
)
doc_rule["allOf"] = [
    {
        "if": {"properties": {"action": {"enum": actions}}},
        "then": {"required": required},
    }
    for actions, required in [
        (["copy-file", "copy-tree", "replace-file"], ["source"]),
        (["add-page", "release-notes"], ["fragment", "entity", "include_file", "selector"]),
        (
            ["insert-before", "insert-after", "append", "prepend", "replace"],
            ["selector", "fragment"],
        ),
        (["remove"], ["selector"]),
        (["set-text"], ["selector", "text"]),
        (["set-attribute"], ["selector", "attribute", "text"]),
        (["exact-text"], ["sha256", "old", "text"]),
    ]
]
glossary = obj({"revision": S, "terms": mapping()})
translation_segment = obj(
    {
        "id": S,
        "path": S,
        "context": S,
        "source": S,
        "source_hash": HASH,
        "placeholders": mapping(),
        "translation": {"type": ["string", "null"]},
        "state": {"enum": ["missing", "translated", "reviewed", "stale", "failed"]},
        "provenance": mapping(),
    }
)
translations = document(
    "pg_maker/translations/v1",
    "TranslationBundle",
    obj(
        {
            "source_tree_hash": HASH,
            "source_language": S,
            "target_language": S,
            "context": mapping(),
            "glossary": glossary,
            "segments": arr(translation_segment),
        }
    ),
    True,
)
doc_rules_list = arr({"oneOf": [S, doc_rules]})


source = {
    "oneOf": [
        obj({"kind": {"const": "git"}, "repository": S, "ref": S}),
        obj(
            {"kind": {"const": "archive"}, "location": S, "sha256": HASH, "strip_components": N},
            ["kind", "location", "sha256"],
        ),
        obj({"kind": {"const": "snapshot"}, "path": S, "include": arr(), "exclude": arr()}),
    ]
}
build_options = obj(
    {
        "configure_args": arr(),
        "defines": mapping({"type": ["string", "integer", "null"]}),
        "cppflags": arr(),
        "cflags": arr(),
        "cxxflags": arr(),
        "ldflags": arr(),
        "options": mapping(B),
    },
    [],
)
build = obj(
    {
        "backend": {"enum": ["autotools", "meson"]},
        "mode": {"enum": ["release", "debug"]},
        "options": mapping(B),
        "nls_languages": arr(),
        "message_locale": S,
        "jobs": P,
        "flags": obj({"cppflags": arr(), "ldflags": arr(), "lto": B}),
    }
)
validation = obj(
    {
        "profile": {"enum": ["smoke", "full", "release"]},
        "run_db_tests": B,
        "baseline": {"enum": ["auto", True, False]},
        "database_locales": arr(),
    },
    [],
)
execution = obj(
    {
        "runner": {"const": "docker"},
        "max_parallel_targets": P,
        "build_user": {"type": "string", "pattern": "^[1-9][0-9]*:[1-9][0-9]*$"},
        "limits": obj({"memory_bytes": P, "min_free_disk_bytes": P, "stage_timeout_seconds": P}),
        "keep_failed_environment": B,
    }
)
vendor = document(
    "pg_maker/vendor/v1",
    "VendorConfig",
    obj(
        {
            "company": S,
            "brand": S,
            "product_name": S,
            "description": S,
            "slug": ID,
            "homepage": {"type": ["string", "null"]},
            "documentation_url": {"type": ["string", "null"]},
            "contact_email": {"type": "string", "pattern": "^[^\\s<>@]+@[^\\s<>@]+$"},
        }
    ),
)
defaults = document(
    "pg_maker/defaults/v1",
    "MakerDefaults",
    obj(
        {
            "paths": obj({"runtime_prefix": S, "data_directory": S, "documentation_directory": S}),
            "service": obj(
                {
                    "user": ID,
                    "group": ID,
                    "home": S,
                    "port": {"type": "integer", "minimum": 1, "maximum": 65535},
                }
            ),
            "naming": obj(
                {
                    key: S
                    for key in ("package", "service", "deb_version", "rpm_version", "rpm_release")
                }
            ),
            "common_changes": arr(obj({"builtin": {"const": "unix-socket-directory"}})),
            "build": build,
            "build_modes": obj(
                {
                    mode: obj(
                        {
                            "naming_suffix": S,
                            "configure_options": arr(),
                            "cflags": arr(),
                            "cppflags": arr(),
                        }
                    )
                    for mode in ("release", "debug")
                }
            ),
            "validation": validation,
            "execution": execution,
        }
    ),
)
feature = document(
    "pg_maker/feature/v1",
    "FeatureProfile",
    obj(
        {
            "description": S,
            "numeric_id": {"type": ["integer", "null"], "minimum": 0},
            "changes": arr(),
            "components": arr(),
            "build_options": build_options,
            "documentation": obj({"rules": doc_rules_list}),
            "required_tests": arr(),
            "compatible_postgresql": S,
        },
        [
            "description",
            "numeric_id",
            "changes",
            "components",
            "build_options",
            "documentation",
            "required_tests",
        ],
    ),
    True,
)
documentation = document(
    "pg_maker/documentation/v1",
    "DocumentationProfile",
    obj(
        {
            "enabled": B,
            "source": {"const": "postgres-tree"},
            "source_language": S,
            "languages": arr(),
            "formats": arr({"enum": ["html", "man", "pdf"]}),
            "html_backend": {"enum": ["docbook", "sphinx"]},
            "branding": {"enum": ["vendor-metadata", "none"]},
            "rules": doc_rules_list,
            "translations": arr(
                {
                    "oneOf": [
                        S,
                        obj(
                            {"input": S, "sha256": HASH, "language": S, "feature": S, "target": S},
                            ["input", "sha256", "language", "feature"],
                        ),
                    ]
                }
            ),
            "missing_translation": {"enum": ["error", "fallback"]},
            "install": obj({"html": S, "man": S}),
            "language_aliases": mapping(),
            "requested_languages": arr(),
            "require_reviewed": B,
            "glossary": glossary,
        },
        [
            "enabled",
            "source",
            "source_language",
            "languages",
            "formats",
            "html_backend",
            "branding",
            "rules",
            "translations",
            "missing_translation",
            "install",
        ],
    ),
)
target = obj(
    {
        "id": ID,
        "platform": S,
        "architecture": {"enum": ["x86_64", "aarch64", "amd64", "arm64"]},
        "cpu_target": S,
        "artifacts": arr({"enum": ["deb", "rpm", "tar", "oci", "deb-source"]}),
    }
)
manifest = obj(
    {
        "api_version": {"const": "pg_maker/v1"},
        "kind": {"const": "PostgreSQLBuild"},
        "metadata": obj({"name": ID}),
        "spec": obj(
            {
                "config_dir": S,
                "source": source,
                "postgresql": obj({"version": S}),
                "product": obj(
                    {"release": N, "package_revision": P, "version": S},
                    ["release", "package_revision"],
                ),
                "features": {**arr(S), "minItems": 1, "uniqueItems": True},
                "targets": {**arr(target), "minItems": 1},
                "documentation": obj({"profile": S, "enabled": B, "languages": arr()}, []),
                "build": obj(build["properties"], []),
                "validation": validation,
                "execution": obj(execution["properties"], []),
                "extra_build_args": arr(),
                "coinstall": B,
                "previous_releases": arr(
                    obj(
                        {
                            "variant": S,
                            "postgresql_version": S,
                            "packages": {
                                **arr(
                                    obj(
                                        {
                                            "role": {"enum": ["server", "client", "libpq"]},
                                            "path": S,
                                            "sha256": HASH,
                                        }
                                    )
                                ),
                                "minItems": 3,
                                "maxItems": 3,
                            },
                        }
                    )
                ),
            },
            ["source", "postgresql", "product", "features", "targets"],
        ),
    }
)
environment = obj({"image": S, "packages": arr(), "tools": mapping(arr())})
platform = document(
    "pg_maker/platform/v1",
    "PlatformRecipe",
    obj(
        {
            "replace_builtin": B,
            "os": obj({"id": S, "version": S}),
            "architectures": arr(),
            "cpu_targets": mapping(mapping(arr())),
            "architecture_names": mapping(),
            "package_format": {"enum": ["deb", "rpm"]},
            "package_manager": S,
            "package_suffix": S,
            "package_filename": S,
            "artifacts": arr(),
            "roles": mapping(obj({"abi_suffix": S})),
            "backends": arr(),
            "compatible_postgresql": S,
            "options": mapping(arr(B)),
            "option_configure": mapping(obj({"enabled": S, "disabled": S})),
            "option_dependencies": mapping(arr()),
            "option_runtime_dependencies": mapping(arr()),
            "runtime_dependencies": mapping(arr()),
            "service_dependencies": mapping(arr()),
            "documentation": obj(
                {
                    "languages": mapping(arr()),
                    "packages": arr(),
                    "tools": mapping(arr()),
                    "format_packages": mapping(arr()),
                    "backend_packages": mapping(arr()),
                }
            ),
            "environments": obj({"build": environment, "test": environment}),
            "test_profiles": arr(),
        },
        [
            "os",
            "architectures",
            "cpu_targets",
            "architecture_names",
            "package_format",
            "package_manager",
            "package_suffix",
            "package_filename",
            "artifacts",
            "roles",
            "backends",
            "compatible_postgresql",
            "options",
            "option_configure",
            "option_dependencies",
            "runtime_dependencies",
            "environments",
            "test_profiles",
        ],
    ),
    True,
)
component = document(
    "pg_maker/component/v1",
    "ComponentRecipe",
    obj(
        {
            "type": {"enum": ["contrib", "pgxs", "source-tree", "utility", "library"]},
            "source": source,
            "compatible_postgresql": S,
            "requires": arr(),
            "conflicts": arr(),
            "source_directory": S,
            "build_dependencies": arr(),
            "runtime_dependencies": arr(),
            "configure_args": arr(),
            "make_targets": arr(),
            "test_targets": arr(),
            "install_targets": arr(),
            "required_tests": arr(),
            "runtime_files": arr(),
            "sql_extension": {"type": ["string", "null"]},
            "shared_library": {"type": ["string", "null"]},
            "preload": arr(),
            "installed_checks": arr(
                obj(
                    {"argv": arr(), "timeout_seconds": P, "stdout_contains": S},
                    ["argv", "timeout_seconds"],
                )
            ),
            "documentation": obj({"rules": doc_rules_list}),
        },
        [
            "type",
            "compatible_postgresql",
            "requires",
            "conflicts",
            "build_dependencies",
            "runtime_dependencies",
            "make_targets",
            "test_targets",
            "install_targets",
            "required_tests",
            "runtime_files",
        ],
    ),
    True,
)
assertion = obj(
    {
        "id": ID,
        "source": {"enum": ["server-log", "stdout", "stderr", "sql", "sqlstate"]},
        "contains": S,
        "equals": {"type": ["string", "integer", "boolean"]},
    },
    ["id", "source"],
)
expectation = obj(
    {"outcome": {"enum": ["pass", "assertion-failure"]}, "assertion_id": ID}, ["outcome"]
)
acceptance = obj(
    {
        "id": ID,
        "requirement_ids": arr(),
        "kind": {"enum": ["sql-and-server-log", "sql", "command"]},
        "setup": obj({"cluster": {"const": "fresh"}, "database": S}),
        "script": S,
        "assertions": arr(assertion),
        "timeout_seconds": P,
        "expectations": obj({"candidate": expectation, "baseline": expectation}, ["candidate"]),
    }
)
changes = document(
    "pg_maker/change-set/v1",
    "ChangeSet",
    obj(
        {
            "compatible_postgresql": S,
            "requires": arr(),
            "conflicts": arr(),
            "operations": arr(
                obj({"id": ID, "kind": {"enum": ["insert_text", "patch"]}, "file": S})
            ),
            "acceptance": arr(acceptance),
            "compatibility": obj(
                {
                    key: {"enum": ["unchanged", "changed", "unknown"]}
                    for key in ("data_format", "wal_format", "extension_abi")
                }
            ),
            "postconditions": obj({"socket_directory": S}, []),
        },
        [
            "compatible_postgresql",
            "requires",
            "conflicts",
            "operations",
            "acceptance",
            "compatibility",
        ],
    ),
    True,
)
insertions = obj(
    {
        "api_version": {"const": "pg_maker/insertions/v1"},
        "kind": {"const": "TextInsertions"},
        "target_file": S,
        "input_sha256": {"anyOf": [HASH, {"type": "null"}]},
        "modifications": arr(
            obj(
                {
                    "id": ID,
                    "top": S,
                    "bot": S,
                    "alignment": {"enum": ["top", "bot"]},
                    "expect_matches": P,
                    "txt": S,
                },
                ["id", "top", "bot", "alignment", "txt"],
            )
        ),
    }
)
postgresql_defaults = obj(
    {
        "source_backends": arr(),
        "execution_backends": arr(),
        "autoconf_regenerate": S,
        "maker_make_minimum": S,
        "documentation": obj(
            {
                "format": {"const": "docbook-xml-4.5"},
                **{
                    key: S
                    for key in (
                        "source_directory",
                        "entrypoint",
                        "generated_variable",
                        "check_target",
                        "html_target",
                        "man_target",
                        "pdf_target",
                        "pdf_tool",
                    )
                },
                "tools": arr(),
            }
        ),
    }
)
postgresql_major = obj(
    {
        "c_standard": {"enum": ["c99", "c11"]},
        "tools": obj(
            {**{key: S for key in ("make", "perl", "bison")}, "flex": {"type": ["string", "null"]}}
        ),
        "generators_required": {"enum": ["always", "git-or-modified-inputs"]},
        "llvm_minimum": S,
        "meson_minimum": {"type": ["string", "null"]},
        "unavailable_options": arr(),
        "extra_tests": arr(),
        "db_test_adapter": {"enum": ["unavailable", "pg18"]},
        "documentation_input": {"enum": ["direct-entities", "expanded-xml"]},
        "documentation_fixes": arr(obj({"compatible_postgresql": S, "document": doc_rules})),
        "reference": S,
    }
)
postgresql_catalog = document(
    "pg_maker/postgresql-catalog/v1",
    "PostgreSQLCatalog",
    obj(
        {
            "defaults": postgresql_defaults,
            "majors": {**mapping(postgresql_major), "propertyNames": {"pattern": "^[1-9][0-9]+$"}},
        }
    ),
    True,
)
postgresql_recipe = obj(
    {
        "major": {"type": "integer", "minimum": 14},
        "revision": P,
        **postgresql_defaults["properties"],
        **postgresql_major["properties"],
    }
)
definitions = {
    "SourceSpec": source,
    "VendorConfig": vendor,
    "MakerDefaults": defaults,
    "FeatureProfile": feature,
    "DocumentationProfile": documentation,
    "DocumentationRules": doc_rules,
    "TranslationBundle": translations,
    "PostgreSQLBuild": manifest,
    "PlatformRecipe": platform,
    "ComponentRecipe": component,
    "ChangeSet": changes,
    "TextInsertions": insertions,
    "PostgreSQLCatalog": postgresql_catalog,
}


def write(name, body):
    value = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"urn:pg_maker:schema:{name}:v1",
        **body,
    }
    (ROOT / f"{name}.schema.json").write_text(json.dumps(value, indent=2) + "\n")


commit = {"type": "string", "pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"}
submodule = obj({"path": S, "commit": commit, "submodules": arr(ref("Submodule"))})
resolved_source = {
    "oneOf": [
        obj(
            {
                "kind": {"const": "git"},
                "commit": commit,
                "submodules": arr(ref("Submodule")),
                "source_hash": HASH,
            }
        ),
        obj(
            {
                "kind": {"const": "snapshot"},
                "selection": obj(
                    {
                        "algorithm": {"const": "fnmatchcase-posix/v1"},
                        "include": arr(),
                        "exclude": arr(),
                    }
                ),
                "source_hash": HASH,
            }
        ),
        obj(
            {
                "kind": {"const": "archive"},
                "archive_sha256": HASH,
                "strip_components": N,
                "source_hash": HASH,
            }
        ),
    ]
}
file_input = obj({"input": S, "sha256": HASH})
resolved_changes = copy.deepcopy(changes)
change_fields = resolved_changes["properties"]["spec"]["properties"]
change_fields["operations"]["items"]["properties"]["file"] = file_input
change_fields["acceptance"]["items"]["properties"]["script"] = file_input
resolved_component = copy.deepcopy(component)
resolved_component["properties"]["spec"]["properties"]["source"] = resolved_source
resolved_feature = copy.deepcopy(feature)
resolved_feature["properties"]["spec"]["properties"].update(
    {"changes": arr(resolved_changes), "components": arr(resolved_component)}
)
identity = obj(
    {
        "policy_version": {"enum": ["pg_maker/naming/v1", "pg_maker/naming/v2"]},
        "product_version": S,
        "service": S,
        "paths": defaults["properties"]["spec"]["properties"]["paths"],
        "packages": arr(
            obj(
                {
                    key: S
                    for key in (
                        "role",
                        "name",
                        "filename",
                        "native_arch",
                        "deb_version",
                        "rpm_version",
                        "rpm_release",
                    )
                }
            )
        ),
        "oci_tag": S,
    }
)
resolved_build = copy.deepcopy(build)
resolved_build["properties"].update(
    {
        "resolved_flags": obj(
            {key: arr() for key in ("cflags", "cppflags", "cxxflags", "ldflags")}
        ),
        "configure_args": arr(),
    }
)
resolved_build["required"] += ["resolved_flags", "configure_args"]
resolved_target = copy.deepcopy(target)
resolved_target["properties"]["platform"] = platform
variant = obj(
    {
        "id": S,
        "feature": ID,
        "target": ID,
        "build": resolved_build,
        "identity": identity,
        "socket_directory": S,
        "baseline": B,
    }
)
effective_config = obj(
    {
        "name": ID,
        "source": resolved_source,
        "postgresql": obj({"version": S}),
        "postgresql_recipe": postgresql_recipe,
        "product": manifest["properties"]["spec"]["properties"]["product"],
        "vendor": vendor["properties"]["spec"],
        "defaults": defaults["properties"]["spec"],
        "documentation": documentation["properties"]["spec"],
        "features": arr(resolved_feature),
        "targets": arr(resolved_target),
        "variants": arr(variant),
        "previous_releases": arr(
            obj(
                {
                    "variant": S,
                    "postgresql_version": S,
                    "packages": arr(
                        obj(
                            {
                                "role": {"enum": ["server", "client", "libpq"]},
                                "input": S,
                                "sha256": HASH,
                            }
                        )
                    ),
                }
            )
        ),
    }
)
effective_config["required"].remove("previous_releases")
image = obj(
    {
        "id": S,
        "target": ID,
        "role": {"enum": ["build", "test"]},
        "image_id": HASH,
        "base_image_id": HASH,
        "base_registry_digests": arr(),
        "registry_manifest_digest": {"anyOf": [HASH, {"type": "null"}]},
        "image_archive_sha256": {"anyOf": [HASH, {"type": "null"}]},
        "os": obj({"id": S, "version": S}),
        "architecture": S,
        "packages": arr(obj({"name": S, "version": S, "architecture": S})),
        "tools": mapping(),
        "method": {"const": "apt-commit"},
        "reproducibility": S,
    }
)
environment_spec = obj({"requirements_hash": HASH, "images": {**arr(image), "minItems": 1}})
image["properties"]["runtime_repository"] = {
    "anyOf": [
        obj(
            {
                "path": S,
                "files": arr(obj({"path": S, "sha256": HASH, "size_bytes": N})),
                "repository_hash": HASH,
            }
        ),
        {"type": "null"},
    ]
}
implementation = obj({"version": S, "code_hash": HASH})
lock_spec = obj(
    {
        "config": effective_config,
        "environments": obj({"environment_hash": HASH, "spec": environment_spec}),
        "blobs": mapping(HASH),
        "implementation": implementation,
    }
)
lock_spec["properties"]["development"] = obj(
    {
        "session_id": S,
        "base_lock": HASH,
        "parent_lock": HASH,
        "trees": mapping(
            obj(
                {
                    "head": S,
                    "source_hash": HASH,
                    "dirty": B,
                    "base": S,
                    "series": arr(obj({"id": S, "commit": S})),
                }
            )
        ),
    }
)
path_ref = obj({"root": {"enum": ["source", "build", "output"]}, "path": S})
step = obj(
    {
        "id": S,
        "kind": S,
        "adapter": S,
        "requires": arr(),
        "target_id": ID,
        "feature_id": ID,
        "branch": {"enum": ["candidate", "baseline"]},
        "environment_id": S,
        "image_id": HASH,
        "user": {"type": "string", "pattern": "^[0-9]+:[0-9]+$"},
        "cwd": path_ref,
        "argv": arr(),
        "env": mapping(),
        "parameters": {"type": "object"},
        "inputs": arr(),
        "outputs": arr(path_ref),
        "checks": arr(),
        "timeout_seconds": P,
        "resources": obj({"memory_bytes": P, "cpu_jobs": P}),
        "cache": {"enum": ["never", "verified-outputs"]},
        "recovery": {"const": "rerun-in-fresh-environment"},
        "stage_key": HASH,
    }
)
plan_spec = obj(
    {
        "lock_hash": HASH,
        "goal": {
            "enum": [
                "source",
                "build",
                "package",
                "verified",
                "docs",
                "translations",
                "doc-source",
                "translated-docs",
            ]
        },
        "run_db_tests": B,
        "implementation": implementation,
        "execution_available": {"type": "boolean"},
        "execution_requires": {"const": None},
        "steps": {**arr(step), "minItems": 1},
    }
)
plan_spec["properties"]["components"] = {"anyOf": [arr(), {"type": "null"}]}
definitions.update(
    {
        "Submodule": submodule,
        "ResolvedSource": resolved_source,
        "EffectiveConfig": effective_config,
        "StepSpec": step,
    }
)
write("planning-common", {"$defs": definitions})
for name, definition in {
    "build": "PostgreSQLBuild",
    "vendor": "VendorConfig",
    "defaults": "MakerDefaults",
    "feature": "FeatureProfile",
    "documentation": "DocumentationProfile",
    "doc-rules": "DocumentationRules",
    "translations": "TranslationBundle",
    "platform": "PlatformRecipe",
    "postgresql-catalog": "PostgreSQLCatalog",
    "component": "ComponentRecipe",
    "change-set": "ChangeSet",
    "insertions": "TextInsertions",
}.items():
    write(name, ref(definition))
for name, version, kind, hash_field in [
    ("environment", "pg_maker/environment/v1", "EnvironmentManifest", "environment_hash"),
    ("lock", "pg_maker/lock/v1", "BuildLock", "lock_hash"),
    ("plan", "pg_maker/plan/v1", "BuildPlan", "plan_hash"),
]:
    write(
        name,
        obj(
            {
                "api_version": {"const": version},
                "kind": {"const": kind},
                "spec": {"environment": environment_spec, "lock": lock_spec, "plan": plan_spec}[
                    name
                ],
                hash_field: HASH,
                "transport": mapping(),
                "created_at": S,
            }
        ),
    )
