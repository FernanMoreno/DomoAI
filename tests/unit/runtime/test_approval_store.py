from datetime import UTC, datetime

import pytest

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Command,
    Plan,
    PlanStatus,
    PolicyAction,
    PolicyDecision,
    ValidationResult,
    ValidationStatus,
)
from domoai.runtime.approval_store import ApprovalStore

OPERATOR_TOKEN = "test-operator-secret"


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
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
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
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)

    consumed = store.consume(grant.approval_id, plan)

    assert consumed.approved_by == "operator"
    assert consumed.plan_id == "plan-1"


def test_consume_rejects_unknown_approval_id() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError):
        store.consume("does-not-exist", plan)


def test_consume_is_single_use() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)
    store.consume(grant.approval_id, plan)

    with pytest.raises(DomainError):
        store.consume(grant.approval_id, plan)


def test_consume_rejects_plan_id_mismatch() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
    plan = _plan_requiring_confirmation()
    grant = store.issue(plan, approved_by="operator", operator_token=OPERATOR_TOKEN)
    other_plan = _plan_requiring_confirmation(plan_id="plan-2")

    with pytest.raises(DomainError):
        store.consume(grant.approval_id, other_plan)


def test_consume_rejects_digest_mismatch_after_revalidation() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
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


def test_issue_refuses_when_configured_token_is_blank() -> None:
    store = ApprovalStore(operator_token="   ")
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token="   ")

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_issue_refuses_when_supplied_token_does_not_match() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
    plan = _plan_requiring_confirmation()

    with pytest.raises(DomainError) as excinfo:
        store.issue(plan, approved_by="operator", operator_token="wrong-token")

    assert excinfo.value.code == ErrorCode.OPERATOR_AUTHENTICATION_FAILED


def test_issue_succeeds_when_supplied_token_matches() -> None:
    store = ApprovalStore(operator_token=OPERATOR_TOKEN)
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
