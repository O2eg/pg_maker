"""One product description for native packagers; no vendor or OS-specific branches."""

from string import Template

from pg_maker.config import resource_path
from pg_maker.errors import ValidationError


def field(value: str) -> str:
    if not value or any(c in value for c in "\x00\r\n"):
        raise ValidationError("package control value must be a nonempty single line")
    return value


def package_spec(config, variant, package, dependencies, tree) -> dict:
    """Identity owns names/native versions, recipe owns dependency names, tree owns files."""
    vendor = config["vendor"]
    role = package["role"]
    result = {
        "api_version": "pg_maker/package-spec/v1",
        "identity": dict(package),
        "metadata": {
            "summary": vendor["description"] + " PostgreSQL " + role,
            "maintainer": vendor["company"] + " <" + vendor["contact_email"] + ">",
            "homepage": vendor.get("homepage"),
            "vendor": vendor["company"],
        },
        "dependencies": list(dependencies),
        "paths": dict(variant["identity"]["paths"]),
        "service": variant["identity"]["service"] if role == "server" else None,
        "install_tree_hash": tree["tree_hash"],
        "files": [e for e in tree["entries"] if e["role"] == role],
    }
    if role == "server" and config.get("documentation", {}).get("enabled", False):
        docs = next(p for p in variant["identity"]["packages"] if p["role"] == "docs")
        result["suggests"] = [docs["name"] + " (= " + docs["deb_version"] + ")"]
    return result


def deb_control(spec) -> tuple[str, dict]:
    identity, metadata = spec["identity"], spec["metadata"]
    values = {
        "Package": identity["name"],
        "Version": identity["deb_version"],
        "Architecture": identity["native_arch"],
        "Section": "database",
        "Priority": "optional",
        "Maintainer": metadata["maintainer"],
        "Description": metadata["summary"],
    }
    if spec["dependencies"]:
        values["Depends"] = ", ".join(spec["dependencies"])
    if metadata["homepage"]:
        values["Homepage"] = metadata["homepage"]
    if spec.get("suggests"):
        values["Suggests"] = ", ".join(spec["suggests"])
    checked = {key: field(value) for key, value in values.items()}
    optional = "".join(
        key + ": " + checked[key] + "\n"
        for key in ("Depends", "Suggests", "Homepage")
        if key in checked
    )
    template = resource_path("packaging", "templates", "deb-control.in").read_text()
    return Template(template).substitute(checked, optional_fields=optional), values
