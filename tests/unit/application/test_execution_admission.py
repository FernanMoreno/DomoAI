from datetime import UTC, datetime

import pytest

from domoai.application.execution_admission import AdmissionOperation, ExecutionAdmission
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Approval,
    BundleCommit,
    BundleMemberCommit,
    Command,
    Plan,
    PlanStatus,
    ValidationResult,
    ValidationStatus,
)
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog


class _BundleRepository:
    def __init__(self, bundle: BundleCommit) -> None:
        self.bundle = bundle

    async def get_for_plan(self, plan_id: str) -> BundleCommit | None:
        return (
            self.bundle
            if any(member.plan_id == plan_id for member in self.bundle.members)
            else None
        )


def _approved_confirmation_plan() -> tuple[Plan, ApprovalStore]:
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    plan = Plan(
        id="plan-admission-authority-1",
        status=PlanStatus.REQUIRES_CONFIRMATION,
        validation=ValidationResult(
            status=ValidationStatus.REQUIRES_CONFIRMATION,
            validated_at=now,
            runtime_revision="runtime-1",
            digest="sha256:validation-authority-1",
            valid_until=datetime(2026, 8, 24, 13, tzinfo=UTC),
        ),
        commands=[
            Command(
                id="command-admission-authority-1",
                device_id="cover.garage",
                command="open",
                idempotency_key="intent-admission-authority-1",
            )
        ],
    )
    store = ApprovalStore(
        operator_token="operator", allow_legacy_token=True, clock=FixedClock(now)
    )
    grant = store.issue(plan, approved_by="operator", operator_token="operator")
    store.consume(grant.approval_id, plan)
    approval = Approval(
        status="approved",
        approved_by=grant.approved_by,
        approved_at=grant.issued_at,
        validation_digest=grant.validation_digest,
        authentication_context=grant.authentication_context,
        session_id=grant.session_id,
        validation_valid_until=grant.validation_valid_until,
        expires_at=grant.expires_at,
        window_digest=grant.window_digest,
        schedule_revision=grant.schedule_revision,
        approval_id=grant.approval_id,
    )
    return plan.model_copy(update={"status": PlanStatus.APPROVED, "approval": approval}), store


def _bundle_member_plan_and_bundle(
    *, plan_id: str = "plan-admission-member-1"
) -> tuple[Plan, BundleCommit]:
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    plan = Plan(
        id=plan_id,
        commands=[
            Command(
                id=f"command-{plan_id}",
                device_id="battery.one",
                command="stop",
                idempotency_key=f"intent-{plan_id}",
            )
        ],
    )
    bundle = BundleCommit(
        id="bundle-admission-member-1",
        bundle_digest="sha256:member-bundle",
        scenario_id="admission-member",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:member-validation",
                execute_at=now,
            )
        ],
    )
    return plan, bundle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "error_code"),
    [
        (AdmissionOperation.EXECUTE, ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN),
        (AdmissionOperation.SCHEDULE, ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN),
        (AdmissionOperation.CANCEL, ErrorCode.BUNDLE_MEMBER_CANCEL_FORBIDDEN),
        (AdmissionOperation.RESCHEDULE, ErrorCode.BUNDLE_MEMBER_RESCHEDULE_FORBIDDEN),
    ],
)
async def test_generic_bundle_member_operations_use_operation_error_mapping(
    operation: AdmissionOperation, error_code: ErrorCode
) -> None:
    plan, bundle = _bundle_member_plan_and_bundle()

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(bundle_repository=_BundleRepository(bundle)).admit(
            plan, operation=operation
        )

    assert excinfo.value.code is error_code


@pytest.mark.asyncio
async def test_admission_default_operation_remains_execute() -> None:
    plan, bundle = _bundle_member_plan_and_bundle()
    admission = ExecutionAdmission(bundle_repository=_BundleRepository(bundle))

    with pytest.raises(DomainError) as default_excinfo:
        await admission.admit(plan, aggregate_owner=False)
    with pytest.raises(DomainError) as execute_excinfo:
        await admission.admit(plan, operation=AdmissionOperation.EXECUTE, aggregate_owner=False)

    assert default_excinfo.value.code is execute_excinfo.value.code
    assert default_excinfo.value.code is ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN


@pytest.mark.asyncio
async def test_bundle_aggregate_owner_uses_owner_checks_not_generic_membership_error() -> None:
    approved, store = _approved_confirmation_plan()
    bundle = BundleCommit(
        id="bundle-admission-authority-1",
        bundle_digest="sha256:expected-bundle",
        scenario_id="admission-authority",
        members=[
            BundleMemberCommit(
                plan_id=approved.id,
                validation_digest="sha256:validation-authority-1",
            )
        ],
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(
            bundle_repository=_BundleRepository(bundle), approval_store=store
        ).admit(approved, aggregate_owner=True)

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED
    assert excinfo.value.code is not ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN


@pytest.mark.asyncio
async def test_bundle_member_rejection_audit_is_operation_scoped_and_credential_free() -> None:
    plan, bundle = _bundle_member_plan_and_bundle()
    audit = AuditLog()

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(
            bundle_repository=_BundleRepository(bundle), audit=audit
        ).admit(plan, operation=AdmissionOperation.CANCEL)

    assert excinfo.value.code is ErrorCode.BUNDLE_MEMBER_CANCEL_FORBIDDEN
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.event_type == "execution_admission_rejected"
    assert event.payload == {
        "operation": AdmissionOperation.CANCEL.value,
        "plan_id": plan.id,
        "bundle_id": bundle.id,
        "error_code": ErrorCode.BUNDLE_MEMBER_CANCEL_FORBIDDEN.value,
        "reason": "Bundle members must execute through the bundle aggregate",
    }
    assert "approval_id" not in event.payload
    assert "token" not in event.payload


@pytest.mark.asyncio
async def test_bundle_admission_rejects_approval_scoped_to_another_bundle() -> None:
    plan_id = "plan-admission-scope-1"
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    plan = Plan(
        id=plan_id,
        status=PlanStatus.APPROVED,
        approval=Approval(
            status="approved",
            approved_by="operator",
            approved_at=now,
            validation_digest="sha256:validation",
            scope="bundle",
            bundle_digest="sha256:other-bundle",
            expires_at=datetime(2026, 8, 24, 12, 5, tzinfo=UTC),
        ),
        commands=[
            Command(
                id="command-admission-scope-1",
                device_id="battery.one",
                command="stop",
                idempotency_key="intent-admission-scope-1",
            )
        ],
    )
    bundle = BundleCommit(
        id="bundle-admission-scope-1",
        bundle_digest="sha256:expected-bundle",
        scenario_id="admission-scope",
        members=[BundleMemberCommit(plan_id=plan_id, validation_digest="sha256:validation")],
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(bundle_repository=_BundleRepository(bundle)).admit(
            plan, aggregate_owner=True
        )

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_admission_fails_closed_when_bundle_scope_cannot_be_verified() -> None:
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    plan = Plan(
        id="plan-admission-no-repository",
        status=PlanStatus.APPROVED,
        approval=Approval(
            status="approved",
            approved_by="operator",
            approved_at=now,
            validation_digest="sha256:validation",
            scope="bundle",
            bundle_digest="sha256:bundle",
            expires_at=datetime(2026, 8, 24, 12, 5, tzinfo=UTC),
        ),
        commands=[
            Command(
                id="command-admission-no-repository",
                device_id="battery.one",
                command="stop",
                idempotency_key="intent-admission-no-repository",
            )
        ],
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission().admit(plan)

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_admission_requires_authoritative_consumed_grant_for_approved_plan() -> None:
    approved, store = _approved_confirmation_plan()

    decision = await ExecutionAdmission(approval_store=store).admit(approved)

    assert decision.plan_id == approved.id


@pytest.mark.asyncio
async def test_admission_rejects_approved_plan_with_forged_grant_projection() -> None:
    approved, store = _approved_confirmation_plan()
    assert approved.approval is not None
    forged = approved.model_copy(
        update={
            "approval": approved.approval.model_copy(
                update={"approval_id": "not-issued-by-the-server"}
            )
        }
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(approval_store=store).admit(forged)

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_admission_rejects_approved_plan_with_non_confirmation_validation() -> None:
    approved, _store = _approved_confirmation_plan()
    assert approved.validation is not None
    inconsistent = approved.model_copy(
        update={
            "approval": None,
            "validation": approved.validation.model_copy(update={"status": ValidationStatus.VALID}),
        }
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(approval_store=ApprovalStore()).admit(inconsistent)

    assert excinfo.value.code is ErrorCode.APPROVAL_REQUIRED
