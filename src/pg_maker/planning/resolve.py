"""Materialize all stage-3 inputs and seal a location-independent BuildLock."""

import copy
from pathlib import Path

from pg_maker.changes.loader import required_tests
from pg_maker.config import environment_requirements
from pg_maker.errors import PreconditionError
from pg_maker.planning.documents import implementation_identity, seal, verify
from pg_maker.sources.resolver import SourceResolver, verify_postgresql_version
from pg_maker.sources.store import InputStore
from pg_maker.storage import content_hash


def resolve_inputs(request, context) -> dict:
    environment = request.environments
    verify(environment, "environment")
    requirements = environment_requirements(request.config)
    if environment["spec"]["requirements_hash"] != content_hash(requirements):
        raise PreconditionError("prepared environments are stale; run env prepare")
    expected = {item["id"]: item for item in requirements["environments"]}
    if {item["id"] for item in environment["spec"]["images"]} != set(expected):
        raise PreconditionError("prepared environment set differs from config")
    for image in environment["spec"]["images"]:
        requirement = expected[image["id"]]
        if image["os"] != requirement["os"] or image["architecture"] != requirement["architecture"]:
            raise PreconditionError("prepared environment platform differs from config")
    store = InputStore(Path(request.input_store))
    resolver = SourceResolver(
        store,
        context.attempt_directory / "inputs",
        offline=request.offline,
        cancelled=context.token.requested,
        on_process=context.on_process,
    )
    config = copy.deepcopy(request.config)
    missing, blobs = [], {}
    from pg_maker.documentation.config import references

    for reference in references(config):
        key, digest = reference["input"], reference["sha256"]
        if request.offline:
            try:
                store.blob(digest)
            except PreconditionError:
                missing.append(key)
        else:
            store.put_blob(Path(request.bindings[key]), digest)
        blobs[key] = digest
    for release in config.get("previous_releases", []):
        for package in release["packages"]:
            key, digest = package["input"], package["sha256"]
            if request.offline:
                try:
                    store.blob(digest)
                except PreconditionError:
                    missing.append(key)
            else:
                store.put_blob(Path(request.bindings[key]), digest)
            blobs[key] = digest
    sources = [(config, "source", Path(request.bindings["source"]), "postgresql")]
    for feature in config["features"]:
        fid = feature["metadata"]["id"]
        required_tests(feature)
        for component in feature["spec"]["components"]:
            if "source" in component["spec"]:
                key = fid + "/components/" + component["metadata"]["id"]
                sources.append((component["spec"], "source", Path(request.bindings[key]), key))
        for change in feature["spec"]["changes"]:
            references = [op["file"] for op in change["spec"]["operations"]]
            references += [test["script"] for test in change["spec"]["acceptance"]]
            for reference in references:
                key = fid + "/" + reference["input"]
                digest = reference["sha256"]
                if request.offline:
                    try:
                        store.blob(digest)
                    except PreconditionError:
                        missing.append(key)
                else:
                    store.put_blob(Path(request.bindings[key]), digest)
                blobs[key] = digest
    for parent, key, base, identifier in sources:
        try:
            parent[key] = resolver.resolve(parent[key], base)
            context.advance("source_resolved")
        except PreconditionError as exc:
            if request.offline and "missing" in exc.details:
                missing.append(identifier)
            else:
                raise
    if missing:
        raise PreconditionError(
            "offline inputs are missing", details={"missing": sorted(set(missing))}
        )
    verify_postgresql_version(
        store.tree(config["source"]["source_hash"]), config["postgresql"]["version"]
    )
    if config["defaults"]["common_changes"]:
        from pg_maker.changes.apply import socket_patch

        patch = context.directory / "unix-socket-directory.patch"
        patch.write_text(socket_patch(store.tree(config["source"]["source_hash"])))
        blobs["builtin/unix-socket-directory"] = store.put_blob(patch)
    return seal(
        "lock",
        {
            "config": config,
            "environments": {
                "environment_hash": environment["environment_hash"],
                "spec": environment["spec"],
            },
            "blobs": blobs,
            "implementation": implementation_identity(),
        },
        {"input_store": str(store.root)},
    )
