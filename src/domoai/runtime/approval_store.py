"""Server-authoritative approval issuance and consumption.

``execute_plan`` must never accept an approval object constructed by an MCP
caller. ``ApprovalStore`` is the only place an ``ApprovalGrant`` can be
created; callers reference it by an opaque, single-use ``approval_id``.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Plan, PlanStatus
from domoai.runtime.clock import Clock, SystemClock


@dataclass(frozen=True)
class OperatorPrincipal:
    """Authenticated operator identity supplied by a trusted host boundary."""

    id: str
    authentication_context: str
    session_id: str

    def __post_init__(self) -> None:
        if not self.id or not self.authentication_context or not self.session_id:
            raise ValueError("operator principal fields must be non-empty")


OperatorPrincipalProvider = Callable[[], OperatorPrincipal | None]


@dataclass(frozen=True)
class ApprovalAssertion:
    """Trusted-host proof that a human approved one exact intent."""

    principal: OperatorPrincipal
    nonce: str
    approved_at: datetime
    expires_at: datetime
    plan_id: str | None = None
    validation_digest: str | None = None
    bundle_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.nonce.strip():
            raise ValueError("approval assertion nonce must be non-empty")
        if self.plan_id is None and self.validation_digest is None and self.bundle_digest is None:
            raise ValueError("approval assertion must bind a plan or bundle digest")
        for name, value in (
            ("approved_at", self.approved_at),
            ("expires_at", self.expires_at),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"approval assertion {name} must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval assertion expiry must follow approval time")


OperatorApprovalAssertionProvider = Callable[[str, str, str | None], ApprovalAssertion | None]


@dataclass(frozen=True)
class ApprovalGrant:
    """An operator-issued, single-use, digest-bound approval record."""

    approval_id: str
    plan_id: str
    validation_digest: str
    approved_by: str
    issued_at: datetime
    authentication_context: str = "legacy_bearer_token"
    session_id: str | None = None
    bundle_digest: str | None = None
    window_digest: str | None = None
    schedule_revision: int = 0
    assertion_nonce: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None


class ApprovalStore:
    """In-process store of pending and consumed approval grants."""

    def __init__(
        self,
        *,
        operator_token: str | None = None,
        allow_legacy_token: bool = False,
        legacy_operator_id: str = "legacy_operator",
        clock: Clock | None = None,
    ) -> None:
        self._grants: dict[str, ApprovalGrant] = {}
        self._consumed: set[str] = set()
        self._assertion_nonces: set[str] = set()
        stripped = operator_token.strip() if operator_token is not None else ""
        self._operator_token: str | None = stripped or None
        self._allow_legacy_token = allow_legacy_token
        self._legacy_operator_id = legacy_operator_id
        self._clock = clock or SystemClock()

    def issue(
        self,
        plan: Plan,
        *,
        approved_by: str,
        operator_token: str | None,
        bundle_digest: str | None = None,
    ) -> ApprovalGrant:
        if (
            not self._allow_legacy_token
            or self._operator_token is None
            or not isinstance(operator_token, str)
            or not hmac.compare_digest(operator_token, self._operator_token)
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
        return self._issue(
            plan,
            approved_by=approved_by,
            authentication_context="legacy_bearer_token",
            session_id=None,
            bundle_digest=bundle_digest,
        )

    def issue_legacy(
        self, plan: Plan, *, operator_token: str | None, bundle_digest: str | None = None
    ) -> ApprovalGrant:
        """Issue local/dev compatibility approval with server-owned identity."""

        return self.issue(
            plan,
            approved_by=self._legacy_operator_id,
            operator_token=operator_token,
            bundle_digest=bundle_digest,
        )

    def issue_authenticated(
        self,
        plan: Plan,
        *,
        principal: OperatorPrincipal,
        assertion: ApprovalAssertion | None = None,
        bundle_digest: str | None = None,
    ) -> ApprovalGrant:
        """Issue a grant only after a trusted host supplies a human assertion."""

        if assertion is None:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_REQUIRED,
                "An authenticated operator principal is not human consent",
            )
        if assertion.principal != principal:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_INVALID,
                "Approval assertion principal does not match the authenticated principal",
            )
        return self.issue_assertion(
            plan,
            assertion=assertion,
            bundle_digest=bundle_digest,
        )

    def issue_assertion(
        self,
        plan: Plan,
        *,
        assertion: ApprovalAssertion,
        bundle_digest: str | None = None,
    ) -> ApprovalGrant:
        """Issue a digest-bound, expiring, one-nonce approval grant."""

        self._assert_issueable(plan)
        now = self._clock.now()
        if assertion.expires_at <= now:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_EXPIRED,
                "Approval assertion has expired",
            )
        if assertion.approved_at > now + timedelta(seconds=30):
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_INVALID,
                "Approval assertion timestamp is in the future",
            )
        if assertion.nonce in self._assertion_nonces:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_REPLAYED,
                "Approval assertion nonce has already been used",
            )
        assert plan.validation is not None
        if assertion.plan_id is not None and assertion.plan_id != plan.id:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_INVALID,
                "Approval assertion does not match the plan",
            )
        if (
            assertion.validation_digest is not None
            and assertion.validation_digest != plan.validation.digest
        ):
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_INVALID,
                "Approval assertion does not match the validation digest",
            )
        if assertion.bundle_digest != bundle_digest:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_INVALID,
                "Approval assertion does not match the bundle digest",
            )

        grant = self._issue(
            plan,
            approved_by=assertion.principal.id,
            authentication_context=assertion.principal.authentication_context,
            session_id=assertion.principal.session_id,
            bundle_digest=bundle_digest,
            assertion_nonce=assertion.nonce,
            approved_at=assertion.approved_at,
            expires_at=assertion.expires_at,
        )
        self._assertion_nonces.add(assertion.nonce)
        return grant

    @staticmethod
    def _assert_issueable(plan: Plan) -> None:
        if plan.status is not PlanStatus.REQUIRES_CONFIRMATION or plan.validation is None:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Only a validated plan requiring confirmation can receive an approval grant",
            )

    def _issue(
        self,
        plan: Plan,
        *,
        approved_by: str,
        authentication_context: str,
        session_id: str | None,
        bundle_digest: str | None,
        assertion_nonce: str | None = None,
        approved_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalGrant:
        self._assert_issueable(plan)
        assert plan.validation is not None
        grant = ApprovalGrant(
            approval_id=uuid.uuid4().hex,
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            approved_by=approved_by,
            issued_at=self._clock.now(),
            authentication_context=authentication_context,
            session_id=session_id,
            bundle_digest=bundle_digest,
            window_digest=plan.execution_window.digest if plan.execution_window else None,
            schedule_revision=plan.schedule_revision,
            assertion_nonce=assertion_nonce,
            approved_at=approved_at,
            expires_at=expires_at,
        )
        self._grants[grant.approval_id] = grant
        return grant

    def consume(
        self, approval_id: str, plan: Plan, *, bundle_digest: str | None = None
    ) -> ApprovalGrant:
        grant = self.validate(approval_id, plan, bundle_digest=bundle_digest)
        self._consumed.add(approval_id)
        return grant

    def validate(
        self, approval_id: str, plan: Plan, *, bundle_digest: str | None = None
    ) -> ApprovalGrant:
        """Validate a grant without consuming it before a bundle preflight ends."""

        grant = self._grants.get(approval_id)
        if grant is None:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Unknown approval")
        if approval_id in self._consumed:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval has already been consumed")
        if grant.expires_at is not None and self._clock.now() >= grant.expires_at:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_EXPIRED,
                "Approval grant has expired",
            )
        if grant.plan_id != plan.id:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval does not match the plan")
        if plan.validation is None or grant.validation_digest != plan.validation.digest:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval does not match the plan's current validation digest",
            )
        expected_window_digest = plan.execution_window.digest if plan.execution_window else None
        if (
            grant.window_digest != expected_window_digest
            or grant.schedule_revision != plan.schedule_revision
        ):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval does not match the plan execution window",
            )
        if grant.bundle_digest != bundle_digest:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval does not match the expected bundle digest",
            )
        return grant
