"""Redaction of structured diagnostics and bounded command-output streams."""

from __future__ import annotations

import os
import re
from typing import Any

from pg_maker.errors import PreconditionError

_SENSITIVE = re.compile(r"password|passwd|secret|token|credential|api[_-]?key", re.I)
_ASSIGNMENT = re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)([^\s,;]+)")
_URL = re.compile(r"(\w+://[^\s/:@]+:)[^\s/@]+(@)")


def sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE.search(name))


def environment_secrets() -> tuple[str, ...]:
    return tuple(value for key, value in os.environ.items() if sensitive_name(key) and value)


def redact_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in sorted(set(secrets or environment_secrets()), key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _URL.sub(r"\1[REDACTED]\2", text)
    return _ASSIGNMENT.sub(r"\1[REDACTED]", text)


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if sensitive_name(str(key)) else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, secrets) for item in value]
    return redact_text(value, secrets) if isinstance(value, str) else value


def resolve_secrets(references: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for name, source in references.items():
        value = os.environ.get(source)
        if not value:
            raise PreconditionError(f"required secret environment variable is missing: {source}")
        if "\x00" in value:
            raise PreconditionError(f"secret environment variable contains NUL: {source}")
        values[name] = value
    return values


class StreamRedactor:
    """Keep only a bounded suffix; mask literal secrets even across read boundaries."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        self.secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))
        self.buffer = ""
        self.window = max((len(s) for s in self.secrets), default=1)

    def feed(self, text: str, *, final: bool = False) -> str:
        self.buffer += text
        cut = len(self.buffer) if final else max(0, len(self.buffer) - self.window)
        for secret in self.secrets:
            start = self.buffer.find(secret, max(0, cut - len(secret) + 1))
            if 0 <= start < cut < start + len(secret):
                cut = start
        result, self.buffer = self.buffer[:cut], self.buffer[cut:]
        for secret in self.secrets:
            result = result.replace(secret, "[REDACTED]")
        return result
