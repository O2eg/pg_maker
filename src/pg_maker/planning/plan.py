"""Pure DAG construction; execution is a separate durable operation."""

from pathlib import Path

from pg_maker.builders.autotools import extra_tests
from pg_maker.changes.loader import required_tests
from pg_maker.errors import PreconditionError, ValidationError
from pg_maker.planning.documents import implementation_identity, seal, store_from_document, verify
from pg_maker.planning.graph import ordered_ids
from pg_maker.planning.reuse import compilation_inputs, selected_components
from pg_maker.storage import content_hash

DOC_GOALS = {"docs", "translations", "doc-source", "translated-docs"}
GOALS = {"source", "build", "package", "verified", *DOC_GOALS}


def build_plan(
    lock: dict,
    *,
    goal: str = "build",
    input_store: Path | None = None,
    run_db_tests: bool | None = None,
    components: list[str] | None = None,
) -> dict:
    verify(lock, "lock")
    if goal not in GOALS:
        raise ValidationError("unknown plan goal")
    if components is not None and (goal != "build" or run_db_tests):
        raise ValidationError("component selection requires goal build with DB tests disabled")
    spec = lock["spec"]
    if spec["implementation"] != implementation_identity():
        raise PreconditionError("maker implementation changed; resolve the inputs again")
    store = store_from_document(lock, input_store)
    config = spec["config"]
    if goal in DOC_GOALS and not config["documentation"]["enabled"]:
        raise ValidationError("documentation goal requires --with-doc before resolve")
    if run_db_tests is not None and type(run_db_tests) is not bool:
        raise ValidationError("run_db_tests must be a boolean")
    if goal == "verified":
        if run_db_tests is False:
            raise ValidationError("verified goal requires DB tests; use goal build or package")
        run_db_tests = True
    elif goal == "source" or goal in DOC_GOALS:
        run_db_tests = False
    elif run_db_tests is None:
        run_db_tests = config["defaults"]["validation"].get("run_db_tests", False)
    if components is not None and run_db_tests:
        raise ValidationError("component selection requires DB tests disabled")
    if spec.get("development") and run_db_tests:
        raise PreconditionError(
            "development snapshots are unverified; "
            "export patches and resolve a release configuration"
        )
    store.tree(config["source"]["source_hash"])
    for digest in spec["blobs"].values():
        store.blob(digest)
    for feature in config["features"]:
        for component in feature["spec"]["components"]:
            if "source" in component["spec"]:
                store.tree(component["spec"]["source"]["source_hash"])
    environment = spec["environments"]
    env_core = {
        "api_version": "pg_maker/environment/v1",
        "kind": "EnvironmentManifest",
        "spec": environment["spec"],
    }
    if content_hash(env_core) != environment["environment_hash"]:
        raise PreconditionError("locked environment hash mismatch")
    images = {item["id"]: item["image_id"] for item in environment["spec"]["images"]}
    steps = []
    limits = config["defaults"]["execution"]["limits"]
    for variant in config["variants"]:
        feature = next(f for f in config["features"] if f["metadata"]["id"] == variant["feature"])
        for branch in (
            ["candidate", "baseline"] if variant["baseline"] and run_db_tests else ["candidate"]
        ):
            tests = required_tests(feature, branch)
            prefix = variant["id"] + "." + branch
            directory = prefix
            previous = []
            core_inputs, component_keys = compilation_inputs(lock, feature, variant, branch)
            selected = selected_components(feature, components)

            def add(
                stage,
                *,
                argv=None,
                env=None,
                checks=(),
                role="build",
                inputs=(),
                outputs=(),
                parameters=None,
                suffix=None,
                reuse_key=None,
                prefix=prefix,
                variant=variant,
                branch=branch,
                directory=directory,
                core_inputs=core_inputs,
            ):
                nonlocal previous
                identifier = prefix + "." + (suffix or stage)
                image_key = variant["target"] + "." + role
                if image_key not in images:
                    raise PreconditionError("prepared environment is missing; run env prepare")
                step = {
                    "id": identifier,
                    "kind": stage,
                    "adapter": "pg_maker/" + stage + "/v1",
                    "requires": previous,
                    "target_id": variant["target"],
                    "feature_id": variant["feature"],
                    "branch": branch,
                    "environment_id": image_key,
                    "image_id": images[image_key],
                    "user": "0:0"
                    if role == "test"
                    else config["defaults"]["execution"]["build_user"],
                    "cwd": {"root": "build", "path": directory + "/obj"},
                    "argv": argv or [],
                    "env": env or {},
                    "parameters": parameters or {},
                    "inputs": list(inputs),
                    "outputs": list(outputs),
                    "checks": list(checks),
                    "timeout_seconds": max(limits["stage_timeout_seconds"], 14400)
                    if stage == "test_source_world"
                    else limits["stage_timeout_seconds"],
                    "resources": {
                        "memory_bytes": limits["memory_bytes"],
                        "cpu_jobs": variant["build"]["jobs"],
                    },
                    "cache": "never"
                    if stage.startswith(("test", "acceptance", "verify"))
                    or stage == "build_and_test_tools"
                    else "verified-outputs",
                    "recovery": "rerun-in-fresh-environment",
                }
                core_stages = {
                    "materialize_source",
                    "apply_changes",
                    "integrate_source_components",
                    "configure",
                    "compile_core",
                    "install_build_prefix",
                    "compile_components",
                    "install_components",
                }
                if reuse_key:
                    identity = {"component": reuse_key, "stage": stage}
                elif stage in core_stages:
                    identity = {"core": core_inputs, "stage": stage}
                else:
                    identity = {"step": step, "lock_hash": lock["lock_hash"]}
                step["stage_key"] = content_hash(identity)
                steps.append(step)
                previous = [identifier]

            build = variant["build"]
            build_root = "/work/build/" + directory
            common = (
                ["builtin/unix-socket-directory"] if config["defaults"]["common_changes"] else []
            )
            changes = common + (
                [c["metadata"]["id"] for c in feature["spec"]["changes"]]
                if branch == "candidate"
                else []
            )
            add(
                "verify_inputs",
                inputs=[config["source"]["source_hash"], lock["lock_hash"]],
                checks=["hashes-match"],
            )
            add(
                "materialize_source",
                parameters={"source": config["source"]},
                outputs=[{"root": "build", "path": directory + "/source"}],
            )
            add(
                "apply_changes",
                parameters={
                    "ordered_changes": changes,
                    "socket_directory": variant["socket_directory"],
                },
                checks=["preconditions", "postconditions"],
            )
            add(
                "integrate_source_components",
                parameters={
                    "components": [
                        c["metadata"]["id"]
                        for c in feature["spec"]["components"]
                        if c["spec"]["type"] == "source-tree"
                    ]
                },
            )
            if goal == "source":
                continue
            env = {
                "LC_ALL": build["message_locale"],
                "PG_TEST_EXTRA": extra_tests(build, config["postgresql_recipe"]),
                **{key.upper(): " ".join(value) for key, value in build["resolved_flags"].items()},
            }
            nls = (
                "--enable-nls=" + " ".join(build["nls_languages"])
                if build["nls_languages"]
                else "--disable-nls"
            )
            add(
                "configure",
                argv=[
                    build_root + "/source/configure",
                    "--prefix=" + variant["identity"]["paths"]["runtime_prefix"],
                    nls,
                    "--enable-tap-tests",
                    *build["configure_args"],
                ],
                env=env,
                checks=["configured-options-match"],
            )
            make = ["make", "--output-sync=target", "-j", str(build["jobs"])]

            def documentation_steps(env=env):
                docs = config["documentation"]
                add(
                    "prepare_documentation",
                    env=env,
                    parameters={"profile": docs},
                    checks=["writer-rules", "docbook-valid"],
                )
                if goal == "doc-source":
                    add("export_documentation", checks=["docs-source-inventory"])
                    return
                for language in docs["languages"]:
                    add(
                        "translate_docs_" + language,
                        env=env,
                        parameters={"language": language, "extract_only": goal == "translations"},
                        checks=["bundle-context", "translation-quality"],
                    )
                    if goal in {"translations", "translated-docs"}:
                        continue
                    add(
                        "build_docs_" + language,
                        env=env,
                        parameters={
                            "language": language,
                            "formats": docs["formats"],
                            "backend": docs["html_backend"],
                        },
                        checks=["offline-assets", "links", "installed-layout"],
                    )
                add("export_documentation", checks=["docs-inventory"])

            if goal in DOC_GOALS:
                documentation_steps()
                continue
            add(
                "compile_core",
                argv=make,
                env=env,
                checks=["command-success", "compiler-warning-inventory"],
            )
            add(
                "install_build_prefix",
                argv=[*make, "install"],
                env=env,
            )
            if run_db_tests:
                add("expected_test_inventory", env=env, checks=["mandatory-suites-present"])
                add(
                    "test_core",
                    argv=[*make, "check"],
                    env=env,
                    checks=["all-tests-pass", "test-log-policy"],
                )
            add(
                "compile_components",
                argv=[*make, "world-bin"],
                env=env,
            )
            add(
                "install_components",
                argv=[*make, "install-world-bin"],
                env=env,
            )
            if run_db_tests and branch == "candidate":
                add(
                    "test_source_world",
                    argv=[*make, "check-world"],
                    env=env,
                    checks=["all-tests-pass", *tests],
                )
            for component in feature["spec"]["components"]:
                component_id = component["metadata"]["id"]
                if component_id not in selected:
                    continue
                add(
                    "build_and_test_tools" if run_db_tests else "build_tools",
                    env=env,
                    parameters={"components": [component], "component_id": component_id},
                    suffix="component-" + component_id,
                    reuse_key=component_keys[component_id],
                    checks=["component-tests-pass"] if run_db_tests else ["command-success"],
                )
            if tests and run_db_tests:
                add(
                    "acceptance_build_prefix",
                    env=env,
                    parameters={"expectations": branch, "changes": feature["spec"]["changes"]},
                    checks=tests,
                )
            if config["documentation"]["enabled"]:
                documentation_steps()
            if goal == "build":
                add("export_build", outputs=[{"root": "output", "path": prefix + "/report.json"}])
                continue
            if run_db_tests:
                add("verify_source", checks=["source-test-evidence"])
            add(
                "collect_install_tree",
                parameters={
                    "identity": variant["identity"],
                    "components": [c["metadata"]["id"] for c in feature["spec"]["components"]],
                    "exclude": ["headers", "pgxs", "ecpg"],
                    "configuration": "initdb-in-pgdata",
                    "file_logs": "pgdata",
                },
                checks=["declared-runtime-files-only"],
            )
            add("runtime_dependencies", checks=["elf-closure", "target-package-database"])
            add(
                "package",
                parameters={
                    "format": "deb",
                    "identity": variant["identity"],
                    "artifact_role": "baseline-package" if branch == "baseline" else "package",
                    "filename_suffix": ".baseline" if branch == "baseline" else "",
                    "suggests_documentation": config["documentation"]["enabled"],
                },
                checks=["package-metadata"],
            )
            if goal == "package":
                if config["documentation"]["enabled"]:
                    add(
                        "test_documentation_packages",
                        role="test",
                        checks=["installed-docs-offline"],
                        parameters={"run_database": False},
                    )
                add(
                    "export",
                    outputs=[{"root": "output", "path": prefix + "/artifact-manifest.json"}],
                )
                continue
            add(
                "test_installed_packages",
                role="test",
                parameters={
                    "profile": config["defaults"]["validation"]["profile"],
                    "service": config["defaults"]["service"],
                    "socket_directory": variant["socket_directory"],
                    "capabilities": [
                        "CHOWN",
                        "DAC_OVERRIDE",
                        "FOWNER",
                        "FSETID",
                        "SETUID",
                        "SETGID",
                        "KILL",
                    ],
                    "service_manager": "emulated",
                },
                checks=[
                    "fresh-install",
                    "service-start-stop",
                    "config-in-pgdata",
                    "file-logs-in-pgdata",
                    "uninstall",
                    "reinstall",
                ],
            )
            if tests:
                add(
                    "acceptance",
                    role="test",
                    parameters={"expectations": branch, "changes": feature["spec"]["changes"]},
                    checks=tests,
                )
            add(
                "export",
                parameters={
                    "publication_eligible": branch == "candidate"
                    and config["defaults"]["validation"]["profile"] in {"full", "release"},
                    "qualification_ceiling": "release-qualified"
                    if branch == "candidate"
                    and config["defaults"]["validation"]["profile"] == "release"
                    else "package-verified",
                    "requires_verification_evidence": True,
                },
                outputs=[{"root": "output", "path": prefix + "/report.json"}],
            )
    ordered_ids(steps)
    return seal(
        "plan",
        {
            "lock_hash": lock["lock_hash"],
            "goal": goal,
            "run_db_tests": run_db_tests,
            "components": components,
            "implementation": spec["implementation"],
            "execution_available": True,
            "execution_requires": None,
            "steps": steps,
        },
        {"input_store": str(store.root)},
    )
