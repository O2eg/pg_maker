"""Compilation identities contain dependencies, not the identity of the entire run."""

from pg_maker.errors import ValidationError
from pg_maker.storage import content_hash


def compilation_inputs(lock, feature, variant, branch="candidate"):
    config = lock["spec"]["config"]
    image = next(
        i
        for i in lock["spec"]["environments"]["spec"]["images"]
        if i["id"] == variant["target"] + ".build"
    )
    changes = feature["spec"]["changes"] if branch == "candidate" else []
    core = {
        "source": config["source"]["source_hash"],
        "changes": [c["spec"]["operations"] for c in changes],
        "common": lock["spec"]["blobs"].get("builtin/unix-socket-directory")
        if config["defaults"]["common_changes"]
        else None,
        "integrated": [
            compilation_recipe(c)
            for c in feature["spec"]["components"]
            if c["spec"]["type"] == "source-tree"
        ],
        "build": variant["build"],
        "image": image["image_id"],
        "target": variant["target"],
        "prefix": variant["identity"]["paths"]["runtime_prefix"],
        "user": config["defaults"]["execution"]["build_user"],
        "implementation": lock["spec"]["implementation"],
    }
    keys = {}
    for component in feature["spec"]["components"]:
        spec, identifier = component["spec"], component["metadata"]["id"]
        keys[identifier] = content_hash(
            {
                "core": core,
                "recipe": compilation_recipe(component),
                "dependencies": {dep: keys[dep] for dep in spec["requires"]},
            }
        )
    return core, keys


def compilation_recipe(component):
    fields = (
        "type",
        "source",
        "source_directory",
        "configure_args",
        "make_targets",
        "install_targets",
        "build_dependencies",
    )
    spec = component["spec"]
    result = {key: spec[key] for key in fields if key in spec}
    if "source" in result:
        result["source"] = result["source"]["source_hash"]
    return {"id": component["metadata"]["id"], **result}


def selected_components(feature, selection):
    recipes = {c["metadata"]["id"]: c for c in feature["spec"]["components"]}
    if selection is None:
        return set(recipes)
    if not selection or set(selection) - recipes.keys():
        raise ValidationError("component selection contains unknown or no components")
    selected = set()

    def visit(identifier):
        if identifier in selected:
            return
        selected.add(identifier)
        for dep in recipes[identifier]["spec"]["requires"]:
            visit(dep)

    for identifier in selection:
        visit(identifier)
    return selected
