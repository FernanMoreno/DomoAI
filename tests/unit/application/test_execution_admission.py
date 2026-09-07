from datetime import UTC, datetime

import pytest

from domoai.application.execution_admission import ExecutionAdmission
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Approval,
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    Plan,
    PlanStatus,
    RiskClass,
    ValidationResult,
    ValidationStatus,
)


class _BundleRepository:
    def __init__(self, bundle: BundleCommit) -> None:
        self.bundle = bundle

    async def get_for_plan(self, plan_id: str) -> BundleCommit | None:
        if any(member.plan_id == plan_id for member in self.bundle.members):
            return self.bundle
        return None


class _EmptyBundleRepository:
    async def get_for_plan(self, plan_id: str) -> BundleCommit | None:
        del plan_id
        return None


def _approved_bundle_member() -> tuple[Plan, BundleCommit]:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    plan = Plan(
        id="bundle-member-approval-scope",
        status=PlanStatus.APPROVED,
        validation=ValidationResult(
            status=ValidationStatus.REQUIRES_CONFIRMATION,
            validated_at=now,
            runtime_revision="runtime-1",
            digest="sha256:validation",
        ),
        approval=Approval(
            status="approved",
            approved_by="operator",
            approved_at=now,
            validation_digest="sha256:validation",
            bundle_digest="sha256:wrong-bundle",
            expires_at=datetime(2026, 8, 25, 12, 5, tzinfo=UTC),
        ),
        commands=[
            Command(
                id="bundle-member-command",
                device_id="light.one",
                command="turn_on",
                risk_class=RiskClass.CONFIRM,
                idempotency_key="bundle-member-intent",
            )
        ],
    )
    bundle = BundleCommit(
        id="bundle-approval-scope",
        bundle_digest="sha256:actual-bundle",
        scenario_id="scenario-approval-scope",
        status=BundleCommitStatus.SCHEDULED,
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:validation",
                status=BundleMemberCommitStatus.EXECUTED,
            )
        ],
    )
    return plan, bundle


@pytest.mark.asyncio
async def test_aggregate_execution_rejects_approval_for_a_different_bundle() -> None:
    plan, bundle = _approved_bundle_member()

    with pytest.raises(DomainError) as error:
        await ExecutionAdmission(bundle_repository=_BundleRepository(bundle)).admit(
            plan, aggregate_owner=True
        )

    assert error.value.code is ErrorCode.APPROVAL_REQUIRED


@pytest.mark.asyncio
async def test_execution_rejects_orphaned_bundle_scoped_approval() -> None:
    plan, _bundle = _approved_bundle_member()

    with pytest.raises(DomainError) as error:
        await ExecutionAdmission(bundle_repository=_EmptyBundleRepository()).admit(
            plan, aggregate_owner=True
        )

    assert error.value.code is ErrorCode.APPROVAL_REQUIRED
