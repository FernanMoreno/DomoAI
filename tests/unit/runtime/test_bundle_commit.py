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
    AggregateExecutionCapability,
    Approval,
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    ExecutionDependencyEvidence,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    ValidationResult,
    ValidationStatus,
    execution_dependency_evidence_digest,
    execution_outcome_digest,
)
from domoai.persistence.repositories import (
    ApprovalGrantRepository,
    BundleCommitRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.events import AuditLog
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics


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
        self.execution_admission = _Admission()

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

    def approve_plan(self, plan: Plan, *, grant) -> Plan:
        assert grant.plan_id == plan.id
        return plan.model_copy(
            update={
                "status": PlanStatus.APPROVED,
                "approval": Approval(
                    status="approved",
                    approved_by=grant.approved_by,
                    approved_at=grant.issued_at,
                    validation_digest=grant.validation_digest,
                    scope="bundle" if grant.bundle_digest else "plan",
                    authentication_context=grant.authentication_context,
                    session_id=grant.session_id,
                    bundle_digest=grant.bundle_digest,
                    recurrence_digest=grant.recurrence_digest,
                    validation_valid_until=grant.validation_valid_until,
                    expires_at=grant.expires_at,
                    window_digest=grant.window_digest,
                    schedule_revision=grant.schedule_revision,
                    approval_id=grant.approval_id,
                    authority=grant.authority,
                ),
            }
        )


class _Admission:
    async def issue_aggregate_capability(
        self, bundle_id: str, member_plan_id: str
    ) -> AggregateExecutionCapability:
        return AggregateExecutionCapability(
            bundle_id=bundle_id,
            member_plan_id=member_plan_id,
            bundle_digest=f"test:{bundle_id}",
            nonce=f"nonce:{bundle_id}:{member_plan_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )


class _PreflightRouteRegistry:
    @staticmethod
    def resolve_command_route(_device_id: str, _command: str):
        return type("Route", (), {"reason": None})()


class _PreflightPlanService:
    registry = _PreflightRouteRegistry()

    @staticmethod
    def assert_executable(_plan: Plan) -> None:
        raise RuntimeError("preflight rejected")


class _PreflightRejectingFacade(_Facade):
    plan_service = _PreflightPlanService()


def test_dependency_evidence_digest_binds_bundle_member_and_outcomes() -> None:
    captured_at = datetime(2026, 8, 30, 12, tzinfo=UTC)
    outcome = ExecutionOutcome(
        plan_id="predecessor",
        command_id="predecessor:command",
        execution_attempt_id="attempt:predecessor",
        status=ExecutionStatus.CONFIRMED_SUCCESS,
        completed_at=captured_at,
    )
    outcome_digest = execution_outcome_digest([outcome])
    evidence_digest = execution_dependency_evidence_digest(
        bundle_id="bundle-1",
        member_plan_id="predecessor",
        predecessor_plan_id="predecessor",
        predecessor_command_ids=[outcome.command_id],
        status=ExecutionStatus.CONFIRMED_SUCCESS,
        state_versions={"light.one::power": 2},
        captured_at=captured_at,
        outcome_digest=outcome_digest,
    )

    evidence = ExecutionDependencyEvidence(
        bundle_id="bundle-1",
        member_plan_id="predecessor",
        predecessor_plan_id="predecessor",
        predecessor_command_ids=[outcome.command_id],
        status=ExecutionStatus.CONFIRMED_SUCCESS,
        state_versions={"light.one::power": 2},
        captured_at=captured_at,
        outcome_digest=outcome_digest,
        evidence_digest=evidence_digest,
    )

    assert evidence.evidence_digest == evidence_digest
    with pytest.raises(ValueError, match="digest mismatch"):
        ExecutionDependencyEvidence(
            **(evidence.model_dump(mode="python") | {"bundle_id": "another-bundle"})
        )


@pytest.mark.asyncio
async def test_commit_records_later_failure_as_partial_and_is_idempotent(tmp_path: Path) -> None:
    _database, bundle_repository, _plan_repository, _scheduled_repository = await _repositories(
        tmp_path
    )
    first = _plan("plan-1", datetime.now(UTC) - timedelta(minutes=1))
    second = _plan("plan-2", datetime.now(UTC) - timedelta(minutes=1))
    facade = _Facade(failures={second.id})
    metrics = RuntimeOperationalMetrics()
    service = BundleCommitService(
        facade=facade,
        plans={first.id: first, second.id: second},
        approval_store=ApprovalStore(operator_token="secret", allow_legacy_token=True),
        bundle_repository=bundle_repository,
        scheduled_repository=_scheduled_repository,
        audit=AuditLog(),
        operational_metrics=metrics,
    )

    result = await service.commit(_request([first, second]))

    assert result.status is BundleCommitStatus.PARTIALLY_COMMITTED
    assert [member.status for member in result.members] == [
        BundleMemberCommitStatus.EXECUTED,
        BundleMemberCommitStatus.UNKNOWN,
    ]
    assert facade.calls == [first.id, second.id]
    assert metrics.snapshot()["bundles"]["partial_total"] == 1

    duplicate = await service.commit(_request([first, second]))
    assert duplicate.id == result.id
    assert duplicate.status is result.status
    assert facade.calls == [first.id, second.id]


@pytest.mark.asyncio
async def test_bundle_commit_consumes_reserved_approval_only_at_commit_point(
    tmp_path: Path,
) -> None:
    _database, bundle_repository, _plan_repository, scheduled_repository = await _repositories(
        tmp_path
    )
    plan = _plan("plan-reserved-commit", datetime.now(UTC) - timedelta(minutes=1)).model_copy(
        update={"status": PlanStatus.REQUIRES_CONFIRMATION}
    )
    approval_store = ApprovalStore(operator_token="secret", allow_legacy_token=True)
    request_member = BundleCommitRequestMember(
        plan_id=plan.id,
        validation_digest=plan.validation.digest if plan.validation else "missing",
        execute_at=plan.execute_at,
        approval_id="pending",
    )
    request = BundleCommitRequest(
        bundle_digest=bundle_approval_digest("scenario-1", [request_member]),
        scenario_id="scenario-1",
        members=[request_member],
    )
    grant = approval_store.issue(
        plan,
        approved_by="operator",
        operator_token="secret",
        bundle_digest=request.bundle_digest,
    )
    request_member = request_member.model_copy(update={"approval_id": grant.approval_id})
    request = request.model_copy(
        update={
            "bundle_digest": bundle_approval_digest("scenario-1", [request_member]),
            "members": [request_member],
        }
    )
    metrics = RuntimeOperationalMetrics()
    service = BundleCommitService(
        facade=_Facade(),
        plans={plan.id: plan},
        approval_store=approval_store,
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
        operational_metrics=metrics,
    )

    result = await service.commit(request)

    assert result.status is BundleCommitStatus.COMPLETED
    assert metrics.snapshot()["bundles"]["completed_total"] == 1
    assert approval_store.verify_consumed(
        service.plans[plan.id], bundle_digest=request.bundle_digest
    )


@pytest.mark.asyncio
async def test_bundle_preflight_failure_releases_reserved_approval(tmp_path: Path) -> None:
    _database, bundle_repository, _plan_repository, scheduled_repository = await _repositories(
        tmp_path
    )
    plan = _plan("plan-reserved-release", datetime.now(UTC) - timedelta(minutes=1)).model_copy(
        update={"status": PlanStatus.REQUIRES_CONFIRMATION}
    )
    approval_store = ApprovalStore(operator_token="secret", allow_legacy_token=True)
    request_member = BundleCommitRequestMember(
        plan_id=plan.id,
        validation_digest=plan.validation.digest if plan.validation else "missing",
        execute_at=plan.execute_at,
        approval_id="pending",
    )
    request = BundleCommitRequest(
        bundle_digest=bundle_approval_digest("scenario-1", [request_member]),
        scenario_id="scenario-1",
        members=[request_member],
    )
    grant = approval_store.issue(
        plan,
        approved_by="operator",
        operator_token="secret",
        bundle_digest=request.bundle_digest,
    )
    request_member = request_member.model_copy(update={"approval_id": grant.approval_id})
    request = request.model_copy(update={"members": [request_member]})
    service = BundleCommitService(
        facade=_PreflightRejectingFacade(),
        plans={plan.id: plan},
        approval_store=approval_store,
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
    )

    result = await service.commit(request)

    assert result.status is BundleCommitStatus.FAILED
    assert (
        approval_store.validate(grant.approval_id, plan, bundle_digest=request.bundle_digest)
        == grant
    )


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

    metrics = RuntimeOperationalMetrics()
    recovered = await BundleRecoveryService(
        bundle_repository=bundle_repository,
        plan_repository=plan_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
        operational_metrics=metrics,
    ).recover_orphaned_bundles()

    assert recovered == [bundle.id]
    persisted = await bundle_repository.get(bundle.id)
    assert persisted is not None
    assert persisted.status is BundleCommitStatus.UNKNOWN
    assert persisted.members[0].status is BundleMemberCommitStatus.UNKNOWN
    assert metrics.snapshot()["bundles"] == {
        "completed_total": 0,
        "partial_total": 0,
        "unknown_total": 1,
        "recovered_total": 1,
    }


@pytest.mark.asyncio
async def test_recovery_commits_reserved_authority_when_bundle_may_have_started(
    tmp_path: Path,
) -> None:
    database, bundle_repository, plan_repository, scheduled_repository = await _repositories(
        tmp_path
    )
    plan = _plan("plan-reservation-recovery", datetime.now(UTC) - timedelta(minutes=1)).model_copy(
        update={"status": PlanStatus.REQUIRES_CONFIRMATION}
    )
    approval_store = ApprovalStore(
        operator_token="secret",
        allow_legacy_token=True,
        persistence=ApprovalGrantRepository(database),
    )
    grant = approval_store.issue(plan, approved_by="operator", operator_token="secret")
    bundle = BundleCommit(
        id="bundle-reservation-recovery",
        bundle_digest="sha256:reservation-recovery",
        scenario_id="scenario-1",
        approval_reservation_id="bundle-reservation-recovery",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:plan-reservation-recovery",
            )
        ],
    )
    approval_store.reserve(
        grant.approval_id,
        plan,
        reservation_id=bundle.id,
        bundle_digest=None,
    )
    await plan_repository.save(plan.model_copy(update={"status": PlanStatus.EXECUTING}))
    await bundle_repository.save(bundle)

    recovered = await BundleRecoveryService(
        bundle_repository=bundle_repository,
        plan_repository=plan_repository,
        scheduled_repository=scheduled_repository,
        audit=AuditLog(),
        approval_store=approval_store,
    ).recover_orphaned_bundles()

    assert recovered == [bundle.id]
    with pytest.raises(DomainError, match="consumed"):
        approval_store.consume(grant.approval_id, plan)


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
