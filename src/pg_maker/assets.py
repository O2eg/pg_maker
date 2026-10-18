"""Idempotent project initialization from installed package resources."""

from importlib.resources import files
from pathlib import Path

from pg_maker.errors import OwnershipError, ValidationError
from pg_maker.storage import ensure_directory, write_text


def initialize_project(
    directory: Path, *, with_doc=None, doc_languages=None, features=None, build_mode=None
) -> dict:
    ensure_directory(directory)
    assets = {"README.md": files("pg_maker").joinpath("bundled", "PROJECT.md").read_text()}
    bundled = files("pg_maker").joinpath("bundled")
    assets["build.yaml"] = bundled.joinpath("build.yaml").read_text()
    for item in bundled.joinpath("config").iterdir():
        if item.name.endswith(".yaml"):
            assets[f"config/{item.name}"] = item.read_text()
    for item in bundled.joinpath("config", "features").iterdir():
        if item.name.endswith(".yaml"):
            assets[f"config/features/{item.name}"] = item.read_text()
    for item in bundled.joinpath("patches").iterdir():
        assets[f"patches/{item.name}"] = item.read_text()

    # Component recipes and their documentation remain editable project inputs.
    def collect(resource, prefix):
        for item in sorted(resource.iterdir(), key=lambda item: item.name):
            name = prefix + "/" + item.name
            if item.is_dir():
                collect(item, name)
            else:
                assets[name] = item.read_text()

    for directory_name in ("components", "documentation"):
        collect(bundled.joinpath(directory_name), directory_name)
    for item in files("pg_maker").joinpath("schemas").iterdir():
        if item.name.endswith(".json"):
            assets[f"schemas/{item.name}"] = item.read_text()
    if any(value is not None for value in (with_doc, doc_languages, features, build_mode)):
        import yaml

        manifest = yaml.safe_load(assets["build.yaml"])
        docs = yaml.safe_load(assets["config/documentation.yaml"])
        defaults = yaml.safe_load(assets["config/defaults.yaml"])
        if with_doc is not None:
            docs["spec"]["enabled"] = with_doc
        if doc_languages is not None:
            if not docs["spec"]["enabled"]:
                raise ValidationError("doc-language requires enabled documentation")
            aliases = docs["spec"].get("language_aliases", {"en": "en", "ru": "ru"})
            if (
                not doc_languages
                or len(set(doc_languages)) != len(doc_languages)
                or any(lang not in aliases for lang in doc_languages)
            ):
                raise ValidationError("unknown or duplicate documentation language")
            docs["spec"]["languages"] = doc_languages
        if features is not None:
            if not features or any(
                "config/features/" + name + ".yaml" not in assets for name in features
            ):
                raise ValidationError("init requires an available feature")
            manifest["spec"]["features"] = features
        if build_mode is not None:
            if build_mode not in {"release", "debug"}:
                raise ValidationError("unknown build mode")
            defaults["spec"]["build"]["mode"] = build_mode
        for name, value in (
            ("build.yaml", manifest),
            ("config/documentation.yaml", docs),
            ("config/defaults.yaml", defaults),
        ):
            assets[name] = yaml.safe_dump(value, sort_keys=False)
    # Preflight every target first: a conflict must not leave a half-updated project.
    for name, text in assets.items():
        target = directory / name
        for item in (target, *target.parents):
            if item.is_symlink():
                raise OwnershipError(f"initialization target traverses a symlink: {item}")
        if any(item.exists() and not item.is_dir() for item in target.parents):
            raise OwnershipError(f"project parent path is not a directory: {target.parent}")
        if target.exists() and (not target.is_file() or target.read_text() != text):
            raise OwnershipError(f"project file already exists with different content: {target}")
    created, unchanged = [], []
    for name, text in assets.items():
        target = directory / name
        if target.exists():
            unchanged.append(name)
        else:
            write_text(target, text)
            created.append(name)
    return {"directory": str(directory), "created": created, "unchanged": unchanged}
