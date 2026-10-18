"""Version and naming policy, independent of platform-specific Python branches."""

import re
import string
from pathlib import PurePosixPath

from pg_maker.errors import ValidationError
from pg_maker.postgresql import version_info

POLICY_VERSION = "pg_maker/naming/v2"


def render(template: str, context: dict[str, str]) -> str:
    pieces = []
    try:
        for literal, field, format_spec, conversion in string.Formatter().parse(template):
            pieces.append(literal)
            if field is not None:
                if field not in context or format_spec or conversion:
                    raise ValidationError(
                        "unsupported naming placeholder", details={"field": field}
                    )
                pieces.append(str(context[field]))
    except ValueError as exc:
        raise ValidationError("malformed naming template") from exc
    result = "".join(pieces)
    if "{" in result or "}" in result or "\x00" in result:
        raise ValidationError("recursive or invalid naming substitution")
    return result


def identities(
    vendor: dict,
    defaults: dict,
    feature: dict,
    target: dict,
    platform: dict,
    version: str,
    product: dict,
    mode: str,
    with_doc: bool,
) -> dict:
    info = version_info(version)
    product_version = f"{info['major']}.{info['minor']}.{product['release']}"
    if info["stage"]:
        product_version += "~" + info["stage"] + info["number"]
    if "version" in product and product["version"] != product_version:
        raise ValidationError("product.version does not match PostgreSQL version and release")
    architecture = platform["architecture_names"][target["architecture"]]
    context = {
        "vendor.slug": vendor["slug"],
        "feature.id": feature["metadata"]["id"],
        "postgresql.major": str(info["major"]),
        "postgresql.version": version,
        "product.version": product_version,
        "package_revision": str(product["package_revision"]),
        "platform.id": platform["id"],
        "platform.package_suffix": platform["package_suffix"],
        "architecture": target["architecture"],
        "build.mode": mode,
        "build.mode_suffix": defaults["build_modes"][mode]["naming_suffix"],
    }
    paths = {name: render(template, context) for name, template in defaults["paths"].items()}
    for name, value in paths.items():
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or str(path) != value or value == "/":
            raise ValidationError(
                "runtime path must be normalized and absolute", details={"field": name}
            )
    service = render(defaults["naming"]["service"], context)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", service):
        raise ValidationError("invalid service name")
    packages = []
    for role in ("server", "client", "libpq", *(("docs",) if with_doc else ())):
        role_context = {
            **context,
            "role": role,
            "role.abi_suffix": platform["roles"][role]["abi_suffix"],
        }
        name = render(defaults["naming"]["package"], role_context)
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", name):
            raise ValidationError("invalid package name", details={"role": role})
        values = {
            key: render(defaults["naming"][key], role_context)
            for key in ("deb_version", "rpm_version", "rpm_release")
        }
        if any(not re.fullmatch(r"[A-Za-z0-9.+~:-]+", val) for val in values.values()):
            raise ValidationError("invalid package version")
        filename = render(
            platform["package_filename"],
            {**role_context, **values, "name": name, "native_arch": architecture},
        )
        if PurePosixPath(filename).name != filename:
            raise ValidationError("package filename contains a path")
        packages.append(
            {
                "role": role,
                "name": name,
                "filename": filename,
                "native_arch": architecture,
                **values,
            }
        )
    if len({p["name"] for p in packages}) != len(packages):
        raise ValidationError("package role naming collision")
    return {
        "policy_version": POLICY_VERSION,
        "product_version": product_version,
        "service": service,
        "paths": paths,
        "packages": packages,
        "oci_tag": (
            f"{product_version.replace('~', '-')}-{platform['package_suffix']}-"
            f"{architecture}{context['build.mode_suffix']}"
        ),
    }
