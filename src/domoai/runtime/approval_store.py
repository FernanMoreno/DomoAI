"""Server-authoritative approval issuance and consumption.

``execute_plan`` must never accept an approval object constructed by an MCP
caller. ``ApprovalStore`` is the only place an ``ApprovalGrant`` can be
created; callers reference it by an opaque, single-use ``approval_id``.
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Plan, PlanStatus


@dataclass(frozen=True)
class ApprovalGrant:
    """An operator-issued, single-use, digest-bound approval record."""

    approval_id: str
    plan_id: str
    validation_digest: str
    approved_by: str
    issued_at: datetime


class ApprovalStore:
    """In-process store of pending and consumed approval grants."""

    def __init__(self, *, operator_token: str | None = None) -> None:
        self._grants: dict[str, ApprovalGrant] = {}
        self._consumed: set[str] = set()
        stripped = operator_token.strip() if operator_token is not None else ""
        self._operator_token: str | None = stripped or None

    def issue(self, plan: Plan, *, approved_by: str, operator_token: str) -> ApprovalGrant:
        if self._operator_token is None or not hmac.compare_digest(
            operator_token, self._operator_token
        ):
            raise DomainError(
                ErrorCode.OPERATOR_AUTHENTICATION_FAILED,
                "Operator approval is not configured or the supplied token is incorrect",
            )
        if plan.status is not PlanStatus.REQUIRES_CONFIRMATION or plan.validation is None:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Only a validated plan requiring confirmation can receive an approval grant",
            )
        grant = ApprovalGrant(
            approval_id=uuid.uuid4().hex,
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            approved_by=approved_by,
            issued_at=datetime.now(UTC),
        )
        self._grants[grant.approval_id] = grant
        return grant

    def consume(self, approval_id: str, plan: Plan) -> ApprovalGrant:
        grant = self._grants.get(approval_id)
        if grant is None:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Unknown approval")
        if approval_id in self._consumed:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval has already been consumed")
        if grant.plan_id != plan.id:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval does not match the plan")
        if plan.validation is None or grant.validation_digest != plan.validation.digest:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval does not match the plan's current validation digest",
            )
        self._consumed.add(approval_id)
        return grant
