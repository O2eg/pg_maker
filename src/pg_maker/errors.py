"""Stable public errors, shared by Python API and CLI."""

from typing import Any


class MakerError(Exception):
    code = "execution_error"
    exit_code = 6

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        from pg_maker.security import redact

        return redact({"code": self.code, "message": str(self), "details": self.details})


class ValidationError(MakerError):
    code = "validation_error"
    exit_code = 2


class PreconditionError(MakerError):
    code = "precondition_failed"
    exit_code = 3


class UnsupportedError(MakerError):
    code = "unsupported"
    exit_code = 4


class CancelledError(MakerError):
    code = "cancelled"
    exit_code = 7


class OwnershipError(MakerError):
    code = "ownership_error"
    exit_code = 8


class StateError(MakerError):
    code = "state_corrupt"


class CommandError(MakerError):
    code = "command_failed"
