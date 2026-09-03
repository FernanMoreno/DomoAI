from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Approval,
    Command,
    ExecutionWindow,
    Plan,
    PlanStatus,
    PolicyAction,
    PolicyDecision,
    ValidationResult,
    ValidationStatus,
)
from domoai.runtime.approval_store import (
    ApprovalAssertion,
    ApprovalStore,
    OperatorPrincipal,
)
from domoai.runtime.clock import FixedClock

OPERATOR_TOKEN = "test-operator-secret"


def _legacy_store() -> ApprovalStore:
    return ApprovalStore(operator_token=OPERATOR_TOKEN, allow_legacy_token=True)


def _plan_requiring_confirmation(plan_id: str = "plan-1", digest: str = "sha256:abc") -> Plan:
    return Plan(
        id=plan_id,
        commands=[
            Command(
                id="cmd-1",
                device_id="cover.garage_main",
                command="open",
                idempotency_key="key-1",
            )
        ],
        status=PlanStatus.REQUIRES_CONFIRMATION,
        validation=ValidationResult(
            status=ValidationStatus.REQUIRES_CONFIRMATION,
            validated_at=datetime.now(UTC),
            runtime_revision="rev-1",
            digest=digest,
        ),
        policy_decisions=[PolicyDecision(action=PolicyAction.CONFIRM, reason="test")],
    )


def test_issue_requires_plan_awaiting_confirmation() -> None:
    store = _legacy_store()
    plan = Plan(
        id="plan-ready",
        commands=[
            Command(
                id="cmd-1",
                device_id="cover.garage_main",
                command="open",
                idempotency_key="key-1",
            )
        ],
        status=PlanStatus.READY,
    )

    with pytest.raises(DomainError):
        store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)


def test_consume_succeeds_for_matching_plan_and_digest() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)

    consumed = store.consume(grant.approval_id, plan)

    assert consumed.approved_by == "operator"
    assert consumed.plan_id == "plan-1"


def test_verify_consumed_accepts_only_the_authoritative_approval_projection() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)
    store.consume(grant.approval_id, plan)
    approved = plan.model_copy(
        update={
            "status": PlanStatus.APPROVED,
            "approval": Approval(
                status="approved",
                approved_by=grant.approved_by,
                approved_at=grant.issued_at,
                validation_digest=grant.validation_digest,
                authentication_context=grant.authentication_context,
                session_id=grant.session_id,
                bundle_digest=grant.bundle_digest,
                recurrence_digest=grant.recurrence_digest,
                validation_valid_until=grant.validation_valid_until,
                expires_at=grant.expires_at,
                window_digest=grant.window_digest,
                schedule_revision=grant.schedule_revision,
                approval_id=grant.approval_id,
            ),
        }
    )

    assert store.verify_consumed(approved) == grant


def test_verify_consumed_rejects_a_forged_approval_projection() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    forged = plan.model_copy(
        update={
            "status": PlanStatus.APPROVED,
            "approval": Approval(
                status="approved",
                approved_by="forged-operator",
                approved_at=datetime.now(UTC),
                validation_digest=plan.validation.digest,
                approval_id="forged-approval-id",
            ),
        }
    )

    with pytest.raises(DomainError) as excinfo:
        store.verify_consumed(forged)

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED


def test_verify_consumed_rejects_a_standing_approval_for_one_off_execution() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    grant = store.issue(
        plan,
        approved_by="operator",
        operator_token=OPERATOR_TOKEN,
        recurrence_digest="sha256:standing-rule",
    )
    store.consume(
        grant.approval_id,
        plan,
        recurrence_digest="sha256:standing-rule",
    )
    approved = plan.model_copy(
        update={
            "status": PlanStatus.APPROVED,
            "approval": Approval(
                status="approved",
                approved_by=grant.approved_by,
                approved_at=grant.issued_at,
                validation_digest=grant.validation_digest,
                scope="recurrence",
                recurrence_digest=grant.recurrence_digest,
                validation_valid_until=grant.validation_valid_until,
                expires_at=grant.expires_at,
                window_digest=grant.window_digest,
                schedule_revision=grant.schedule_revision,
                approval_id=grant.approval_id,
            ),
        }
    )

    with pytest.raises(DomainError) as excinfo:
        store.verify_consumed(approved)

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED


def test_consume_rejects_unknown_approval_id() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError):
        store.consume("does-not-exist", plan)


def test_consume_is_single_use() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)
    store.consume(grant.approval_id, plan)

    with pytest.raises(DomainError):
        store.consume(grant.approval_id, plan)


def test_consume_rejects_plan_id_mismatch() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)
    other_plan = _plan_requiring_confirmation(plan_id="plan-2")

    with pytest.raises(DomainError):
        store.consume(grant.approval_id, other_plan)


def test_consume_rejects_digest_mismatch_after_revalidation() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)
    revalidated_plan = _plan_requiring_confirmation(digest="sha256:def")

    with pytest.raises(DomainError):
        store.consume(grant.approval_id, revalidated_plan)


def test_issue_refuses_when_no_operator_token_configured() -> None:
    store = ApprovalStore()
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token="anything")

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_issue_requires_explicit_legacy_compatibility_mode() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_issue_refuses_when_configured_token_is_blank() -> None:
    store = ApprovalStore(operator_token="   ")
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token="   ")

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_issue_refuses_when_supplied_token_does_not_match() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token="wrong-token")

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_issue_succeeds_when_supplied_token_matches() -> None:
    store = _legacy_store()
    plan = _plan_requiring_confirmation()

    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)

    assert grant.plan_id == "plan-1"
    assert grant.approved_by == "operator"


def test_issue_refuses_even_when_supplied_token_is_empty_string() -> None:
    store = ApprovalStore()
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token="")

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_authenticated_principal_issues_grant_without_bearer_token() -> None:
    issued_at = datetime(2026, 8, 23, 12, tzinfo=UTC)
    store = ApprovalStore(clock=FixedClock(issued_at))
    principal = OperatorPrincipal(
        id="human-operator-42",
        authentication_context="oidc:mfa",
        session_id="session-7",
    )
    assertion = ApprovalAssertion(
        principal=principal,
        plan_id="plan-1",
        validation_digest="sha256:abc",
        nonce="gesture-1",
        approved_at=issued_at,
        expires_at=datetime(2026, 8, 23, 12, 5, tzinfo=UTC),
    )

    grant = store.issue_authenticated(
        _plan_requiring_confirmation(), principal=principal, assertion=assertion
    )

    assert grant.approved_by == principal.id
    assert grant.authentication_context == principal.authentication_context
    assert grant.session_id == principal.session_id
    assert grant.issued_at == issued_at
    assert grant.assertion_nonce == assertion.nonce
    assert grant.expires_at == assertion.expires_at


def test_authenticated_principal_without_human_assertion_is_rejected() -> None:
    store = ApprovalStore(clock=FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))
    principal = OperatorPrincipal(
        id="human-operator-42",
        authentication_context="oidc:mfa",
        session_id="session-7",
    )

    with pytest.raises(DomainError) as excinfo:
        store.issue_authenticated(_plan_requiring_confirmation(), principal=principal)

    assert excinfo.value.code == ErrorCode.APPROVAL_ASSERTION_REQUIRED


def test_assertion_digest_mismatch_is_rejected() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    store = ApprovalStore(clock=FixedClock(now))
    principal = OperatorPrincipal("human", "oidc:mfa", "session")
    assertion = ApprovalAssertion(
        principal=principal,
        plan_id="plan-1",
        validation_digest="sha256:not-the-plan",
        nonce="gesture-digest-mismatch",
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
    )

    with pytest.raises(DomainError) as excinfo:
        store.issue_authenticated(
            _plan_requiring_confirmation(), principal=principal, assertion=assertion
        )

    assert excinfo.value.code == ErrorCode.APPROVAL_ASSERTION_INVALID


def test_assertion_nonce_cannot_issue_two_grants() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    store = ApprovalStore(clock=FixedClock(now))
    principal = OperatorPrincipal("human", "oidc:mfa", "session")
    assertion = ApprovalAssertion(
        principal=principal,
        plan_id="plan-1",
        validation_digest="sha256:abc",
        nonce="gesture-once",
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    plan = _plan_requiring_confirmation()
    store.issue_authenticated(plan, principal=principal, assertion=assertion)

    with pytest.raises(DomainError) as excinfo:
        store.issue_authenticated(plan, principal=principal, assertion=assertion)

    assert excinfo.value.code == ErrorCode.APPROVAL_ASSERTION_REPLAYED


def test_grant_expiry_is_checked_at_consumption() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = ApprovalStore(clock=clock)
    principal = OperatorPrincipal("human", "oidc:mfa", "session")
    plan = _plan_requiring_confirmation()
    grant = store.issue_authenticated(
        plan,
        principal=principal,
        assertion=ApprovalAssertion(
            principal=principal,
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            nonce="gesture-expiry",
            approved_at=now,
            expires_at=now + timedelta(minutes=1),
        ),
    )
    clock.set(now + timedelta(minutes=1))

    with pytest.raises(DomainError) as excinfo:
        store.consume(grant.approval_id, plan)

    assert excinfo.value.code == ErrorCode.APPROVAL_ASSERTION_EXPIRED


def test_bundle_bound_grant_cannot_be_used_for_another_bundle() -> None:
    store = ApprovalStore(clock=FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))
    principal = OperatorPrincipal(
        id="human-operator-42",
        authentication_context="oidc:mfa",
        session_id="session-7",
    )
    plan = _plan_requiring_confirmation()
    grant = store.issue_authenticated(
        plan,
        principal=principal,
        assertion=ApprovalAssertion(
            principal=principal,
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            bundle_digest="sha256:bundle-a",
            nonce="gesture-bundle",
            approved_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
            expires_at=datetime(2026, 8, 23, 12, 5, tzinfo=UTC),
        ),
        bundle_digest="sha256:bundle-a",
    )

    with pytest.raises(DomainError):
        store.validate(grant.approval_id, plan, bundle_digest="sha256:bundle-b")
    with pytest.raises(DomainError):
        store.validate(grant.approval_id, plan)

    assert store.consume(grant.approval_id, plan, bundle_digest="sha256:bundle-a") == grant


def test_approval_grant_is_bound_to_execution_window_and_revision() -> None:
    window = ExecutionWindow(
        intended_at=datetime(2026, 8, 23, 13, tzinfo=UTC),
        not_before=datetime(2026, 8, 23, 12, 59, tzinfo=UTC),
        not_after=datetime(2026, 8, 23, 13, 1, tzinfo=UTC),
        timezone="Europe/Madrid",
        revision=2,
    )
    plan = _plan_requiring_confirmation().model_copy(update={"execution_window": window})
    store = ApprovalStore(clock=FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))
    principal = OperatorPrincipal(
        id="human-operator-42",
        authentication_context="oidc:mfa",
        session_id="session-7",
    )
    grant = store.issue_authenticated(
        plan,
        principal=principal,
        assertion=ApprovalAssertion(
            principal=principal,
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
            nonce="gesture-window",
            approved_at=datetime(2026, 8, 23, 12, tzinfo=UTC),
            expires_at=datetime(2026, 8, 23, 12, 5, tzinfo=UTC),
        ),
    )

    changed_window = window.model_copy(
        update={"intended_at": datetime(2026, 8, 23, 13, 2, tzinfo=UTC)}
    )
    changed_plan = plan.model_copy(update={"execution_window": changed_window})

    with pytest.raises(DomainError, match="execution window"):
        store.validate(grant.approval_id, changed_plan)
