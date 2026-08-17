"""Stable errors raised by domain and runtime validation."""

from enum import StrEnum
from typing import Any

from domoai.domain.models import ErrorDetail


class ErrorCode(StrEnum):
    INVALID_TRANSITION = "invalid_transition"
    INVALID_CAPABILITY = "invalid_capability"
    DEVICE_NOT_FOUND = "device_not_found"
    UNSUPPORTED_COMMAND = "unsupported_command"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    DUPLICATE_COMMAND = "duplicate_command"
    STALE_PLAN = "stale_plan"
    APPROVAL_REQUIRED = "approval_required"
    POLICY_DENIED = "policy_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ROUTE_AMBIGUOUS = "route_ambiguous"
    ROUTE_NOT_FOUND = "route_not_found"
    SOURCE_UNAVAILABLE = "source_unavailable"
    EXECUTION_FAILED = "execution_failed"
    VALIDATION_ERROR = "validation_error"


class DomainError(ValueError):
    """An expected, safe-to-serialize application error."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        field: str | None = None,
        device_id: str | None = None,
        capability: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.device_id = device_id
        self.capability = capability
        self.retryable = retryable
        self.details = details or {}

    def as_detail(self) -> ErrorDetail:
        return ErrorDetail(
            code=self.code,
            message=self.message,
            field=self.field,
            device_id=self.device_id,
            capability=self.capability,
            retryable=self.retryable,
            details=self.details,
        )


class InvalidTransitionError(DomainError):
    def __init__(self, current: str, requested: str) -> None:
        super().__init__(
            ErrorCode.INVALID_TRANSITION,
            f"Transition from {current!r} to {requested!r} is not allowed",
            details={"current": current, "requested": requested},
        )
