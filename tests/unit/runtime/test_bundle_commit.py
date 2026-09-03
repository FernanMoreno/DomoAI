from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.application.bundle_commit import (
    BundleCommitRequest,
    BundleCommitRequestMember,
    BundleCommitService,
    BundleRecoveryService,
    bundle_approval_digest,
)
from domoai.domain.errors import DomainError
from domoai.domain.models import (
    Approval,
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    ValidationResult,
    ValidationStatus,
)
from domoai.persistence.repositories import (
    BundleCommitRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.events import AuditLog


def _plan(plan_id: str, execute_at: datetime | None) -> Plan:
    return Plan(
        id=plan_id,
        execute_at=execute_at,
        status=PlanStatus.READY,
        validation=ValidationResult(
            status=ValidationStatus.VALID,
            validated_at=datetime.now(UTC),
            runtime_revision="runtime-1",
            digest=f"sha256:{plan_id}",
        ),
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="light.one",
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


def _request(plans: list[Plan]) -> BundleCommitRequest:
    members = [
        BundleCommitRequestMember(
            plan_id=plan.id,
            validation_digest=f"sha256:{plan.id}",
            execute_at=plan.execute_at,
        )
        for plan in plans
    ]
    return BundleCommitRequest(
        bundle_digest=bundle_approval_digest("scenario-1", members),
        scenario_id="scenario-1",
        members=members,
    )


async def _repositories(
    tmp_path: Path,
) -> tuple[SQLiteDatabase, BundleCommitRepository, PlanRepository, ScheduledPlanRepository]:
    database = SQLiteDatabase(tmp_path / "bundle.sqlite3")
    await database.initialize()
    return (
        database,
        BundleCommitRepository(database),
        PlanRepository(database),
        ScheduledPlanRepository(database),
    )


class _Facade:
    def __init__(self, failures: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.failures = failures or set()

    async def execute_plan(self, plan: Plan, *, aggregate_owner: bool = False) -> ExecutionSummary:
        assert aggregate_owner is True
        self.calls.append(plan.id)
        if plan.id in self.failures:
            raise RuntimeError(f"failure:{plan.id}")
        return ExecutionSummary(
            outcomes=[
                ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=plan.commands[0].id,
                    execution_attempt_id=f"attempt:{plan.id}",
                    status=ExecutionStatus.CONFIRMED_SUCCESS,
                )
            ]
        )


@pytest.mark.asyncio
async def test_commit_records_later_failure_as_partial_and_is_idempotent(tmp_path: Path) -> None:
    _database, bundle_repository, _plan_repository, _scheduled_repository = await _repositories(
        tmp_path
    )
    first = _plan("plan-1", datetime.now(UTC) - timedelta(minutes=1))
    second = _plan("plan-2", datetime.now(UTC) - timedelta(minutes=1))
    facade = _Facade(failures={second.id})
    service = BundleCommitService(
        facade=facade,
        plans={first.id: first, second.id: second},
        approval_store=ApprovalStore(operator_token="secret", allow_legacy_token=True),
        bundle_repository=bundle_repository,
        scheduled_repository=_scheduled_repository,
        audit=AuditLog(),
    )

    result = await service.commit(_request([first, second]))

    assert result.status is BundleCommitStatus.PARTIALLY_COMMITTED
    assert [member.status for member in result.members] == [
        BundleMemberCommitStatus.EXECUTED,
        BundleMemberCommitStatus.UNKNOWN,
    ]
    assert facade.calls == [first.id, second.id]

    duplicate = await service.commit(_request([first, second]))
    assert duplicate.id == result.id
    assert duplicate.status is result.status
    assert facade.calls == [first.id, second.id]


@pytest.mark.asyncio
async def test_recovery_marks_in_progress_bundle_unknown_without_replay(tmp_path: Path) -> None:
    database, bundle_repository, plan_repository, scheduled_repository = await _repositories(
        tmp_path
    )
    plan = _plan("plan-recovery", datetime.now(UTC) - timedelta(minutes=1))
    await plan_repository.save(plan.model_copy(update={"status": PlanStatus.EXECUTING}))
    bundle = BundleCommit(
        id="bundle-recovery",
        bundle_digest="sha256:recovery",
        scenario_id="scenario-1",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:plan-recovery",
                execute_at=plan.execute_at,
            )
        ],
    )
    await bundle_repository.save(bundle)

    recovered = await BundleRecoveryService(
        bundle_repository=bundle_repository,
        plan_repository=plan_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
    ).recover_orphaned_bundles()

    assert recovered == [bundle.id]
    persisted = await bundle_repository.get(bundle.id)
    assert persisted is not None
    assert persisted.status is BundleCommitStatus.UNKNOWN
    assert persisted.members[0].status is BundleMemberCommitStatus.UNKNOWN


@pytest.mark.asyncio
async def test_future_only_bundle_schedules_all_members_as_one_commit(tmp_path: Path) -> None:
    _database, bundle_repository, _plan_repository, scheduled_repository = await _repositories(
        tmp_path
    )
    first = _plan("plan-future-1", datetime.now(UTC) + timedelta(hours=1))
    second = _plan("plan-future-2", datetime.now(UTC) + timedelta(hours=2))
    service = BundleCommitService(
        facade=_Facade(),
        plans={first.id: first, second.id: second},
        approval_store=ApprovalStore(operator_token="secret", allow_legacy_token=True),
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
    )

    result = await service.commit(_request([first, second]))

    assert result.status is BundleCommitStatus.SCHEDULED
    assert [plan.id for plan in await scheduled_repository.list_pending()] == [
        first.id,
        second.id,
    ]


@pytest.mark.asyncio
async def test_bundle_commit_rejects_existing_approval_for_another_bundle(tmp_path: Path) -> None:
    _database, bundle_repository, _plan_repository, scheduled_repository = await _repositories(
        tmp_path
    )
    plan = _plan("plan-wrong-bundle-approval", datetime.now(UTC) + timedelta(hours=1))
    approved = plan.model_copy(
        update={
            "status": PlanStatus.APPROVED,
            "approval": Approval(
                status="approved",
                approved_by="operator",
                approved_at=datetime.now(UTC),
                validation_digest=plan.validation.digest if plan.validation else "missing",
                scope="bundle",
                bundle_digest="sha256:another-bundle",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            ),
        }
    )
    service = BundleCommitService(
        facade=_Facade(),
        plans={approved.id: approved},
        approval_store=ApprovalStore(operator_token="secret", allow_legacy_token=True),
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
    )

    with pytest.raises(DomainError, match="bundle"):
        await service.commit(_request([approved]))

    assert await scheduled_repository.list_pending() == []
