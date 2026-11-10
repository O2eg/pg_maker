"""Explicit receiver configuration; only credential references are persisted."""

import copy
import re
from pathlib import Path
from urllib.parse import urlsplit

from pg_maker.documents import relative_output, validate_document
from pg_maker.errors import ValidationError
from pg_maker.yamlio import load_yaml


def validate(config):
    config = copy.deepcopy(config)
    for key, default in (
        ("request_timeout_seconds", 30),
        ("verification_timeout_seconds", 300),
        ("poll_interval_seconds", 2),
        ("source_timeout_seconds", 86400),
    ):
        config.setdefault(key, default)
    validate_document(
        {"api_version": "pg_maker/publication/v1", "kind": "PublicationConfig", "spec": config},
        "publication",
    )
    seen = set()
    for receiver in config["repositories"]:
        name = receiver["id"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}", name) or name in seen:
            raise ValidationError("receiver IDs must be unique simple names")
        seen.add(name)
        adapter = receiver["adapter"]
        if receiver.get("path_prefix") and (
            not relative_output(receiver["path_prefix"])
            or not re.fullmatch(r"[A-Za-z0-9_./+-]+", receiver["path_prefix"])
        ):
            raise ValidationError("receiver path_prefix must be a normalized relative path")
        if adapter == "directory":
            if not Path(receiver.get("directory", "")).is_absolute():
                raise ValidationError("directory receiver requires an absolute directory")
            if set(receiver) - {"id", "adapter", "formats", "directory", "path_prefix"}:
                raise ValidationError("directory receiver contains network-only options")
            continue
        if not receiver.get("url") or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", receiver.get("repository", "")
        ):
            raise ValidationError("Nexus requires URL and repository name")
        parsed = urlsplit(receiver["url"])
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or "%" in parsed.path
            or ".." in parsed.path.split("/")
        ):
            raise ValidationError("Nexus URL must contain no credentials, query or traversal")
        if parsed.scheme == "http" and not receiver.get("allow_http", False):
            raise ValidationError("HTTP requires explicit allow_http; HTTPS is the default")
        if adapter == "nexus-apt":
            if receiver["formats"] != ["deb"] or not all(
                re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", receiver.get(k, ""))
                for k in ("suite", "component")
            ):
                raise ValidationError("APT requires deb format, suite and component")
            if "path_prefix" in receiver:
                raise ValidationError("APT chooses package paths from its package metadata")
        elif adapter == "nexus-yum" and receiver["formats"] != ["rpm"]:
            raise ValidationError("YUM accepts RPM packages only")
        if "directory" in receiver or (
            adapter != "nexus-apt" and any(k in receiver for k in ("suite", "component"))
        ):
            raise ValidationError("receiver contains options for another adapter")
        auth = receiver.get("auth", {})
        for key, value in auth.items():
            if key.endswith("_env") and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValidationError("invalid credential environment reference")
        for path in (auth.get("netrc_file"), receiver.get("tls", {}).get("ca_file")):
            if path and not Path(path).is_absolute():
                raise ValidationError("credential and CA file references must be absolute")
    return config


def load(path):
    path = path.expanduser().absolute()
    config = copy.deepcopy(load_yaml(path, "publication")["spec"])
    for receiver in config["repositories"]:
        if "url" in receiver:
            receiver["url"] = receiver["url"].rstrip("/")
        for mapping, key in (
            (receiver, "directory"),
            (receiver.get("auth", {}), "netrc_file"),
            (receiver.get("tls", {}), "ca_file"),
        ):
            if key in mapping:
                value = Path(mapping[key]).expanduser()
                mapping[key] = str((path.parent / value).absolute())
    for key, default in [
        ("request_timeout_seconds", 30),
        ("verification_timeout_seconds", 300),
        ("poll_interval_seconds", 2),
        ("source_timeout_seconds", 86400),
    ]:
        config.setdefault(key, default)
    return validate(config)
