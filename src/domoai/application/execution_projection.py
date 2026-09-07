"""Project adapter outcomes into truthful automation results."""

from __future__ import annotations

from typing import Literal

from domoai.domain.models import ExecutionStatus, ExecutionSummary

ExecutionProjectionStatus = Literal["executed", "failed", "partial", "unknown"]


def project_execution_summary(
    summary: ExecutionSummary,
) -> tuple[ExecutionProjectionStatus, str]:
    """Return the externally visible result of one execution attempt.

    An empty summary contains no evidence of execution.  ``UNKNOWN`` and
    ``UNAVAILABLE`` remain uncertainty, while a mixture of confirmed and
    failed members is reported as partial so callers cannot mistake it for a
    successful bundle.
    """

    statuses = {outcome.status for outcome in summary.outcomes}
    if not statuses:
        return "unknown", "no_execution_outcomes"
    if statuses & {ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE}:
        return "unknown", "execution_unknown"
    failures = statuses & {
        ExecutionStatus.REJECTED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    }
    if failures and ExecutionStatus.CONFIRMED_SUCCESS in statuses:
        return "partial", "execution_partial"
    if failures:
        return "failed", "execution_failed"
    if statuses == {ExecutionStatus.CONFIRMED_SUCCESS}:
        return "executed", "plan_executed"
    return "unknown", "execution_not_terminal"


__all__ = ["ExecutionProjectionStatus", "project_execution_summary"]
