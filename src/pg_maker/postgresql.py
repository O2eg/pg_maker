"""PostgreSQL version syntax and the data-driven major-version build catalogue."""

import re
from importlib.resources import files

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from pg_maker.errors import PreconditionError, UnsupportedError, ValidationError
from pg_maker.yamlio import load_yaml


def version_info(value: str) -> dict:
    match = re.fullmatch(r"([1-9][0-9]*)(?:\.(0|[1-9][0-9]*)|(beta|rc)([1-9][0-9]*))", value)
    if match is None:
        raise ValidationError("PostgreSQL version must be M.m, MbetaN or MrcN")
    major, minor, stage, number = match.groups()
    if int(major) < 14:
        raise UnsupportedError("PostgreSQL versions below 14 are not supported")
    return {"major": int(major), "minor": int(minor or 0), "stage": stage, "number": number}


def recipe(version: str) -> dict:
    info = version_info(version)
    document = load_yaml(
        files("pg_maker").joinpath("recipes/postgresql.yaml"), "postgresql-catalog"
    )
    catalog = document["spec"]
    major = str(info["major"])
    if major not in catalog["majors"]:
        raise UnsupportedError("PostgreSQL major has no build recipe", details={"major": major})
    result = {**catalog["defaults"], **catalog["majors"][major]}
    if result["meson_minimum"] is not None:
        result["source_backends"] = [*result["source_backends"], "meson"]
    return {"major": info["major"], "revision": document["metadata"]["revision"], **result}


def verify_tools(minimums: dict, observed: dict) -> dict:
    """Check recorded container tool output, never the controller's toolchain."""
    versions = {}
    for name, minimum in minimums.items():
        match = re.search(
            r"(?<![0-9])([0-9]+\.[0-9]+(?:\.[0-9]+)*)(?![0-9])", observed.get(name, "")
        )
        if match is None or Version(match[1]) < Version(minimum):
            raise PreconditionError(
                "build tool does not satisfy PostgreSQL recipe",
                details={"tool": name, "minimum": minimum, "observed": observed.get(name)},
            )
        versions[name] = match[1]
    return versions


def documentation_fixes(version: str, selected: dict) -> list[dict]:
    return [
        fix["document"]
        for fix in selected["documentation_fixes"]
        if SpecifierSet(fix["compatible_postgresql"]).contains(version, prereleases=True)
    ]
