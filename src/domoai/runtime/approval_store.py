"""Server-authoritative approval issuance and consumption.

``execute_plan`` must never accept an approval object constructed by an MCP
caller. ``ApprovalStore`` is the only place an ``ApprovalGrant`` can be
created; callers reference it by an opaque, single-use ``approval_id``.
"""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Approval, AuthorityContext, Plan, PlanStatus
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


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
    recurrence_digest: str | None = None

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


class ApprovalGrantPersistence(Protocol):
    """Synchronous persistence hooks used at the authority boundary."""

    def save_sync(self, grant: ApprovalGrant) -> None: ...

    def get_sync(self, approval_id: str) -> ApprovalGrant | None: ...

    def is_pending_sync(self, approval_id: str) -> bool: ...

    def is_consumed_sync(self, approval_id: str) -> bool: ...

    def consume_if_pending_sync(self, approval_id: str, *, now: datetime) -> bool: ...

    def nonce_exists_sync(self, nonce: str) -> bool: ...

    def reservation_status_sync(self, approval_id: str) -> tuple[str, str] | None: ...

    def list_reservation_approval_ids_sync(self, reservation_id: str) -> list[str]: ...

    def reserve_if_pending_sync(
        self, approval_id: str, *, reservation_id: str, now: datetime
    ) -> bool: ...

    def commit_reservation_sync(
        self, approval_id: str, *, reservation_id: str, now: datetime
    ) -> bool: ...

    def commit_reservation_batch_sync(
        self, approval_ids: list[str], *, reservation_id: str, now: datetime
    ) -> bool: ...

    def release_reservation_sync(
        self, approval_id: str, *, reservation_id: str, now: datetime
    ) -> bool: ...


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
    recurrence_digest: str | None = None
    validation_valid_until: datetime | None = None
    window_digest: str | None = None
    schedule_revision: int = 0
    assertion_nonce: str | None = None
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    authority: AuthorityContext = field(default_factory=AuthorityContext)


class ApprovalStore:
    """In-process store of pending and consumed approval grants."""

    APPROVAL_TTL = timedelta(minutes=5)

    def __init__(
        self,
        *,
        operator_token: str | None = None,
        allow_legacy_token: bool = False,
        legacy_operator_id: str = "legacy_operator",
        clock: Clock | None = None,
        persistence: ApprovalGrantPersistence | None = None,
        operational_metrics: RuntimeOperationalMetrics | None = None,
    ) -> None:
        self._grants: dict[str, ApprovalGrant] = {}
        self._consumed: set[str] = set()
        self._reservations: dict[str, tuple[str, str]] = {}
        self._assertion_nonces: set[str] = set()
        stripped = operator_token.strip() if operator_token is not None else ""
        self._operator_token: str | None = stripped or None
        self._allow_legacy_token = allow_legacy_token
        self._legacy_operator_id = legacy_operator_id
        self._clock = clock or SystemClock()
        self._persistence = persistence
        self._operational_metrics = operational_metrics

    def issue(
        self,
        plan: Plan,
        *,
        approved_by: str,
        operator_token: str | None,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
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
        self._assert_issueable(plan, recurrence_digest=recurrence_digest)
        return self._issue(
            plan,
            approved_by=approved_by,
            authentication_context="legacy_bearer_token",
            session_id=None,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
        )

    def issue_legacy(
        self,
        plan: Plan,
        *,
        operator_token: str | None,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> ApprovalGrant:
        """Issue local/dev compatibility approval with server-owned identity."""

        return self.issue(
            plan,
            approved_by=self._legacy_operator_id,
            operator_token=operator_token,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
        )

    def issue_authenticated(
        self,
        plan: Plan,
        *,
        principal: OperatorPrincipal,
        assertion: ApprovalAssertion | None = None,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
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
            recurrence_digest=recurrence_digest,
        )

    def issue_assertion(
        self,
        plan: Plan,
        *,
        assertion: ApprovalAssertion,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> ApprovalGrant:
        """Issue a digest-bound, expiring, one-nonce approval grant."""

        self._assert_issueable(plan, recurrence_digest=recurrence_digest)
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
        if assertion.nonce in self._assertion_nonces or (
            self._persistence is not None and self._persistence.nonce_exists_sync(assertion.nonce)
        ):
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
        if assertion.recurrence_digest != recurrence_digest:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_INVALID,
                "Approval assertion does not match the recurrence digest",
            )

        grant = self._issue(
            plan,
            approved_by=assertion.principal.id,
            authentication_context=assertion.principal.authentication_context,
            session_id=assertion.principal.session_id,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
            assertion_nonce=assertion.nonce,
            approved_at=assertion.approved_at,
            expires_at=assertion.expires_at,
        )
        self._assertion_nonces.add(assertion.nonce)
        return grant

    @staticmethod
    def _assert_issueable(plan: Plan, *, recurrence_digest: str | None = None) -> None:
        standing = recurrence_digest is not None
        allowed = plan.status is PlanStatus.REQUIRES_CONFIRMATION or (
            standing and plan.status in {PlanStatus.READY, PlanStatus.APPROVED}
        )
        if plan.validation is None or not allowed:
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
        recurrence_digest: str | None,
        assertion_nonce: str | None = None,
        approved_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ApprovalGrant:
        self._assert_issueable(plan, recurrence_digest=recurrence_digest)
        assert plan.validation is not None
        now = self._clock.now()
        server_expiry = now + self.APPROVAL_TTL
        requested_expiry = expires_at or server_expiry
        effective_expiry = min(
            requested_expiry,
            server_expiry,
            plan.validation.valid_until or server_expiry,
        )
        if effective_expiry <= now:
            raise DomainError(
                ErrorCode.APPROVAL_ASSERTION_EXPIRED,
                "Approval lifetime is already expired",
            )
        grant = ApprovalGrant(
            approval_id=uuid.uuid4().hex,
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            approved_by=approved_by,
            issued_at=self._clock.now(),
            authentication_context=authentication_context,
            session_id=session_id,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
            validation_valid_until=plan.validation.valid_until,
            window_digest=plan.execution_window.digest if plan.execution_window else None,
            schedule_revision=plan.schedule_revision,
            assertion_nonce=assertion_nonce,
            approved_at=approved_at,
            expires_at=effective_expiry,
        )
        if self._persistence is not None:
            self._persistence.save_sync(grant)
        self._grants[grant.approval_id] = grant
        return grant

    def consume(
        self,
        approval_id: str,
        plan: Plan,
        *,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> ApprovalGrant:
        grant = self.validate(
            approval_id,
            plan,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
        )
        self._consumed.add(approval_id)
        self._record_approval("consumed")
        return grant

    def reserve(
        self,
        approval_id: str,
        plan: Plan,
        *,
        reservation_id: str,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> ApprovalGrant:
        """Hold a grant while a durable commit is assembled.

        Reservation deliberately leaves the grant pending in the approval
        ledger. It becomes consumed only after the caller has durably created
        the schedule/commit record, and can be released on a pre-write
        failure.
        """

        if not reservation_id.strip():
            raise ValueError("approval reservation ID must be non-empty")
        existing = self._reservation_status(approval_id)
        if existing is not None:
            if existing == (reservation_id, "reserved"):
                grant = self._load_grant(approval_id)
                if grant is None:
                    raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Unknown approval")
                self._validate_grant_binding(
                    grant,
                    plan,
                    bundle_digest=bundle_digest,
                    recurrence_digest=recurrence_digest,
                )
                return grant
            if existing[1] != "released":
                raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval is already reserved")
        try:
            grant = self.validate(
                approval_id,
                plan,
                bundle_digest=bundle_digest,
                recurrence_digest=recurrence_digest,
            )
        except DomainError:
            self._record_approval("rejected")
            raise
        if self._persistence is not None and not self._persistence.reserve_if_pending_sync(
            approval_id,
            reservation_id=reservation_id,
            now=self._clock.now(),
        ):
            self._record_approval("rejected")
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval is already reserved")
        self._reservations[approval_id] = (reservation_id, "reserved")
        self._record_approval("reserved")
        return grant

    def commit_reservation(
        self, reservation_id: str, approval_ids: list[str] | None = None
    ) -> None:
        """Atomically consume the grants held by a commit reservation."""

        ids = self._reservation_ids(reservation_id, approval_ids)
        if self._persistence is not None and ids:
            if not self._persistence.commit_reservation_batch_sync(
                ids, reservation_id=reservation_id, now=self._clock.now()
            ):
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Approval reservation could not commit",
                )
            for approval_id in ids:
                self._reservations[approval_id] = (reservation_id, "committed")
                self._consumed.add(approval_id)
                self._record_approval("consumed")
            return
        for approval_id in ids:
            status = self._reservation_status(approval_id)
            if status is None or status[0] != reservation_id:
                raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval reservation is missing")
            if status[1] == "committed":
                self._consumed.add(approval_id)
                continue
            if status[1] != "reserved":
                raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval reservation is not active")
            if self._persistence is not None and not self._persistence.commit_reservation_sync(
                approval_id,
                reservation_id=reservation_id,
                now=self._clock.now(),
            ):
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "Approval reservation could not commit",
                )
            self._reservations[approval_id] = (reservation_id, "committed")
            self._consumed.add(approval_id)
            self._record_approval("consumed")

    def release_reservation(
        self, reservation_id: str, approval_ids: list[str] | None = None
    ) -> None:
        """Release grants held by a commit that failed before physical writes."""

        for approval_id in self._reservation_ids(reservation_id, approval_ids):
            status = self._reservation_status(approval_id)
            if status is None or status[0] != reservation_id or status[1] == "released":
                continue
            if status[1] == "committed":
                continue
            if self._persistence is not None:
                self._persistence.release_reservation_sync(
                    approval_id,
                    reservation_id=reservation_id,
                    now=self._clock.now(),
                )
            self._reservations[approval_id] = (reservation_id, "released")
            self._record_approval("released")

    def _record_approval(self, event: str) -> None:
        if self._operational_metrics is not None:
            self._operational_metrics.record_approval(event)

    def verify_consumed(
        self,
        plan: Plan,
        *,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> ApprovalGrant:
        """Verify that an approved plan is backed by its consumed server grant.

        ``Plan.approval`` is durable execution evidence, not an authority source:
        it can be restored or corrupted independently of the grant ledger.  A
        physical execution therefore needs both the projection and the original
        consumed grant to agree at the last admission boundary.
        """

        approval = plan.approval
        if (
            plan.status is not PlanStatus.APPROVED
            or approval is None
            or approval.status != "approved"
            or approval.approval_id is None
        ):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approved plan is missing authoritative approval evidence",
            )
        grant = self._load_grant(approval.approval_id)
        if grant is None:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approved plan references an unknown approval grant",
            )
        if not self._is_consumed(approval.approval_id):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approved plan references an unconsumed approval grant",
            )
        self._validate_grant_binding(
            grant,
            plan,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
        )
        self._validate_projection(approval, grant)
        return grant

    def validate(
        self,
        approval_id: str,
        plan: Plan,
        *,
        bundle_digest: str | None = None,
        recurrence_digest: str | None = None,
    ) -> ApprovalGrant:
        """Validate a grant without consuming it before a bundle preflight ends."""

        grant = self._load_grant(approval_id)
        if grant is None:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Unknown approval")
        if self._is_consumed(approval_id):
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval has already been consumed")
        reservation = self._reservation_status(approval_id)
        if reservation is not None and reservation[1] == "reserved":
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval is reserved")
        self._validate_grant_binding(
            grant,
            plan,
            bundle_digest=bundle_digest,
            recurrence_digest=recurrence_digest,
        )
        return grant

    def _load_grant(self, approval_id: str) -> ApprovalGrant | None:
        grant = self._grants.get(approval_id)
        if grant is None and self._persistence is not None:
            grant = self._persistence.get_sync(approval_id)
            if grant is not None:
                self._grants[approval_id] = grant
        return grant

    def _is_consumed(self, approval_id: str) -> bool:
        if approval_id in self._consumed:
            return True
        return self._persistence is not None and self._persistence.is_consumed_sync(
            approval_id
        )

    def _reservation_status(self, approval_id: str) -> tuple[str, str] | None:
        local = self._reservations.get(approval_id)
        if local is not None:
            return local
        if self._persistence is not None:
            return self._persistence.reservation_status_sync(approval_id)
        return None

    def _reservation_ids(
        self, reservation_id: str, approval_ids: list[str] | None
    ) -> list[str]:
        ids = list(dict.fromkeys(approval_ids or []))
        ids.extend(
            approval_id
            for approval_id, (held_by, _status) in self._reservations.items()
            if held_by == reservation_id and approval_id not in ids
        )
        if self._persistence is not None:
            ids.extend(
                approval_id
                for approval_id in self._persistence.list_reservation_approval_ids_sync(
                    reservation_id
                )
                if approval_id not in ids
            )
        return ids

    def _validate_grant_binding(
        self,
        grant: ApprovalGrant,
        plan: Plan,
        *,
        bundle_digest: str | None,
        recurrence_digest: str | None,
    ) -> None:
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
        if grant.validation_valid_until != plan.validation.valid_until:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval does not match the current validation evidence lifetime",
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
        if grant.recurrence_digest != recurrence_digest:
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Approval does not match the standing automation recurrence",
            )
        return grant
