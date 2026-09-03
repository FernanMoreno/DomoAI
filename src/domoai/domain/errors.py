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
    APPROVAL_ASSERTION_REQUIRED = "approval_assertion_required"
    APPROVAL_ASSERTION_INVALID = "approval_assertion_invalid"
    APPROVAL_ASSERTION_REPLAYED = "approval_assertion_replayed"
    APPROVAL_ASSERTION_EXPIRED = "approval_assertion_expired"
    OPERATOR_AUTHENTICATION_FAILED = "operator_authentication_failed"
    POLICY_DENIED = "policy_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    ROUTE_AMBIGUOUS = "route_ambiguous"
    ROUTE_NOT_FOUND = "route_not_found"
    SOURCE_UNAVAILABLE = "source_unavailable"
    EXECUTION_FAILED = "execution_failed"
    VALIDATION_ERROR = "validation_error"
    PRECONDITION_FAILED = "precondition_failed"
    NOT_YET_DUE = "not_yet_due"
    SAFETY_LIMIT_EXCEEDED = "safety_limit_exceeded"
    POST_WRITE_RECONCILIATION_FAILED = "post_write_reconciliation_failed"
    PLAN_IDENTITY_CONFLICT = "plan_identity_conflict"
    RESCHEDULE_REQUIRES_REVALIDATION = "reschedule_requires_revalidation"
    BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN = "bundle_member_reschedule_forbidden"
    BUNDLE_MEMBER_EXECUTION_FORBIDDEN = "bundle_member_execution_forbidden"
    BUNDLE_MEMBER_CANCEL_FORBIDDEN = "bundle_member_cancel_forbidden"
    SCHEDULE_EVIDENCE_MISMATCH = "schedule_evidence_mismatch"
    INVALID_COMMAND_VALUE = "invalid_command_value"
    CONTROL_TAKEOVER_FAILED = "control_takeover_failed"
    ACTUATOR_AUTHORIZATION_REQUIRED = "actuator_authorization_required"
    INSUFFICIENT_SCOPE = "insufficient_scope"


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
