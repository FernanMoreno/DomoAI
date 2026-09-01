"""Adversarial composition checks for the physical admission boundary."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.bundle_commit import (
    BundleCommitRequestMember,
    BundleCommitService,
    bundle_approval_digest,
)
from domoai.application.discovery_service import DiscoveryService
from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.scheduler import Scheduler
from domoai.application.state_service import StateService
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    ExecutionStatus,
    Plan,
    PlanStatus,
    Policy,
    PolicyAction,
)
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.persistence.repositories import (
    AuditEventRepository,
    BundleCommitRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
    ScheduledPlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore

OPERATOR_TOKEN = "physical-authority-test-operator"


class _BundleRepository:
    def __init__(self, bundle: BundleCommit) -> None:
        self.bundle = bundle

    async def get_for_plan(self, plan_id: str) -> BundleCommit | None:
        if any(member.plan_id == plan_id for member in self.bundle.members):
            return self.bundle
        return None


def _plan(plan_id: str) -> Plan:
    return Plan(
        id=plan_id,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="battery.one",
                command="stop",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


def _bundle(*members: BundleMemberCommit) -> BundleCommit:
    return BundleCommit(
        id="physical-admission-composition-bundle",
        bundle_digest="sha256:physical-admission-composition",
        scenario_id="physical-admission-composition",
        members=list(members),
    )


def _structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


async def _build_context(
    tmp_path, *, require_confirmation: bool = False, now: datetime
) -> DomoticsMcpContext:
    clock = FixedClock(now)
    adapter = SimulatedHomeAdapter(clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    database = SQLiteDatabase(tmp_path / "physical-authority.sqlite3", clock=clock)
    await database.initialize()
    audit_repository = AuditEventRepository(database)
    audit = AuditLog(sink=audit_repository, clock=clock)
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    policies = (
        [
            Policy(
                id="physical-authority-confirm-brightness",
                target={"capability": "brightness"},
                action=PolicyAction.CONFIRM,
            )
        ]
        if require_confirmation
        else []
    )
    plan_service = PlanService(registry, state_store, PolicyEngine(policies), audit, clock=clock)
    plan_repository = PlanRepository(database, clock=clock)
    scheduled_repository = ScheduledPlanRepository(database, clock=clock)
    bundle_repository = BundleCommitRepository(database, clock=clock)
    approval_store = ApprovalStore(
        operator_token=OPERATOR_TOKEN,
        allow_legacy_token=True,
        clock=clock,
    )
    execution_admission = ExecutionAdmission(
        bundle_repository=bundle_repository,
        approval_store=approval_store,
        audit=audit,
    )
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        outcome_repository=ExecutionOutcomeRepository(database),
        execution_admission=execution_admission,
        clock=clock,
    )
    facade = DomoticsFacade(plan_service, executor)
    scheduler = Scheduler(
        executor,
        scheduled_repository,
        audit,
        bundle_repository=bundle_repository,
        execution_admission=execution_admission,
        clock=clock,
    )
    plans: dict[str, Plan] = {}
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=policies,
        approval_store=approval_store,
        plan_repository=plan_repository,
        plans=plans,
        scheduler=scheduler,
        audit_repository=audit_repository,
        clock=clock,
    )
    context.bundle_commit_service = BundleCommitService(
        facade=facade,
        plans=plans,
        approval_store=approval_store,
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=audit,
        plan_repository=plan_repository,
        clock=clock,
    )
    return context


async def _validated_plan(
    context: DomoticsMcpContext,
    *,
    plan_id: str,
    execute_at: datetime | None = None,
) -> Plan:
    light_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    plan = Plan(
        id=plan_id,
        execute_at=execute_at,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id=light_id,
                command="set_brightness",
                value=60,
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )
    validated = context.facade.validate_plan(plan)
    assert context.plan_repository is not None
    await context.plan_repository.save_validation(validated)
    context.plans[validated.id] = validated
    return validated


def _dependency_member(
    plan_id: str,
    *,
    status: BundleMemberCommitStatus,
    execution_status: ExecutionStatus | None,
) -> BundleMemberCommit:
    details: dict[str, Any] = {}
    if execution_status is ExecutionStatus.CONFIRMED_SUCCESS:
        details = {
            "dependency_evidence": {
                "status": ExecutionStatus.CONFIRMED_SUCCESS.value,
                "captured_at": "2026-08-30T12:00:00+00:00",
            }
        }
    return BundleMemberCommit(
        plan_id=plan_id,
        validation_digest=f"sha256:{plan_id}",
        status=status,
        execution_status=execution_status,
        details=details,
    )


def _confirmed_predecessor(plan_id: str) -> BundleMemberCommit:
    captured_at = datetime(2026, 8, 26, 12, tzinfo=UTC)
    return BundleMemberCommit(
        plan_id=plan_id,
        validation_digest=f"sha256:{plan_id}",
        status=BundleMemberCommitStatus.EXECUTED,
        execution_status=ExecutionStatus.CONFIRMED_SUCCESS,
        details={
            "dependency_evidence": {
                "status": ExecutionStatus.CONFIRMED_SUCCESS.value,
                "captured_at": captured_at.isoformat(),
            }
        },
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_direct_execution_of_bundle_member_is_rejected_before_write() -> None:
    plan = _plan("dependent-direct")
    bundle = _bundle(
        _confirmed_predecessor("predecessor-direct"),
        BundleMemberCommit(
            plan_id=plan.id,
            validation_digest="sha256:dependent-direct",
            predecessor_plan_id="predecessor-direct",
        ),
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(bundle_repository=_BundleRepository(bundle)).admit(plan)

    assert excinfo.value.code is ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN


@pytest.mark.composition
@pytest.mark.asyncio
async def test_fan_in_requires_confirmed_success_from_every_predecessor() -> None:
    plan = _plan("fan-in-dependent")
    bundle = _bundle(
        _confirmed_predecessor("fan-in-success"),
        BundleMemberCommit(
            plan_id="fan-in-failed",
            validation_digest="sha256:fan-in-failed",
            status=BundleMemberCommitStatus.EXECUTED,
            execution_status=ExecutionStatus.FAILED,
            details={"dependency_evidence": {"status": ExecutionStatus.FAILED.value}},
        ),
        BundleMemberCommit(
            plan_id=plan.id,
            validation_digest="sha256:fan-in-dependent",
            predecessor_plan_ids=["fan-in-success", "fan-in-failed"],
        ),
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(bundle_repository=_BundleRepository(bundle)).admit(
            plan, aggregate_owner=True
        )

    assert excinfo.value.code is ErrorCode.PRECONDITION_FAILED
    assert excinfo.value.details["predecessor_plan_id"] == "fan-in-failed"


@pytest.mark.composition
@pytest.mark.asyncio
async def test_mcp_bundle_member_rejection_has_no_physical_or_durable_side_effect(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=UTC)
    context = await _build_context(tmp_path, require_confirmation=True, now=now)
    server = create_domotics_server(context)
    plan = await _validated_plan(
        context,
        plan_id="composition-rejected-member",
        execute_at=now + timedelta(hours=1),
    )
    assert plan.status is PlanStatus.REQUIRES_CONFIRMATION
    assert plan.validation is not None

    bundle_repository = context.bundle_commit_service.bundle_repository  # type: ignore[union-attr]
    scheduled_repository = context.scheduler.repository  # type: ignore[union-attr]
    bundle = BundleCommit(
        id="composition-rejected-member-bundle",
        bundle_digest="sha256:composition-rejected-member-bundle",
        scenario_id="composition-rejected-member-scenario",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest=plan.validation.digest,
                execute_at=plan.execute_at,
            )
        ],
    )
    await bundle_repository.save(bundle)
    bundle_before = await bundle_repository.schedule_members_transaction(
        bundle,
        [plan],
        [0],
        final_status=BundleCommitStatus.SCHEDULED,
    )
    approval = _structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": plan.id,
                "validation_digest": plan.validation.digest,
                "bundle_digest": bundle_before.bundle_digest,
                "operator_token": OPERATOR_TOKEN,
            },
        )
    )
    approval_id = cast(str, approval["approval_id"])
    plan_before = await context.plan_repository.get(plan.id)  # type: ignore[union-attr]
    scheduled_before = await scheduled_repository.get(plan.id)
    assert plan_before == plan
    assert scheduled_before is not None
    assert scheduled_before[1] == "pending"
    audit_log = context.facade.plan_service.audit
    audit_before_ids = {event.id for event in audit_log.events}

    result = _structured(
        await server.call_tool(
            "execute_plan",
            {
                "plan_id": plan.id,
                "validation_digest": plan.validation.digest,
                "approval_id": approval_id,
                "bundle_digest": bundle_before.bundle_digest,
            },
        )
    )

    assert result["error"]["code"] == ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN.value
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    assert adapter.calls == []
    assert plan_before is not None
    assert context.plans[plan.id] == plan_before
    assert await context.plan_repository.get(plan.id) == plan_before  # type: ignore[union-attr]
    assert await scheduled_repository.get(plan.id) == scheduled_before
    assert await bundle_repository.get(bundle_before.id) == bundle_before
    assert (
        await context.facade.executor.outcome_repository.list_for_plan(plan.id)  # type: ignore[union-attr]
        == []
    )
    pending = context.approval_store.validate(
        approval_id,
        plan,
        bundle_digest=bundle_before.bundle_digest,
    )
    assert pending.approval_id == approval_id

    new_events = [event for event in audit_log.events if event.id not in audit_before_ids]
    assert [event.event_type for event in new_events] == [
        "mcp_request_authorized",
        "execution_admission_rejected",
    ]
    rejection = new_events[-1]
    durable_events = await context.audit_repository.list_all()  # type: ignore[union-attr]
    assert any(event.id == rejection.id for event in durable_events)
    assert set(rejection.payload) == {
        "operation",
        "plan_id",
        "bundle_id",
        "error_code",
        "reason",
    }
    assert rejection.payload["operation"] == "execute"
    assert rejection.payload["error_code"] == ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN.value
    assert len(cast(str, rejection.payload["reason"])) <= 200
    serialized = json.dumps(rejection.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    payload_serialized = json.dumps(rejection.payload, sort_keys=True)
    assert len(serialized.encode("utf-8")) <= 1024
    assert OPERATOR_TOKEN not in serialized
    for command in plan.commands:
        assert command.command not in payload_serialized
        if command.value is not None:
            assert str(command.value) not in payload_serialized
    assert all(
        forbidden not in serialized.lower()
        for forbidden in ("approval", "token", "secret", "credential", "command")
    )
    assert not any(
        event.event_type
        in {
            "plan_execution_started",
            "command_execution_outcome",
            "plan_execution_completed",
        }
        for event in new_events
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_oversized_rejection_identifiers_stay_bounded() -> None:
    oversized_plan_id = "plan-" + "p" * 4096
    oversized_bundle_id = "bundle-" + "b" * 4096
    plan = _plan(oversized_plan_id)
    audit = AuditLog()
    bundle = BundleCommit(
        id=oversized_bundle_id,
        bundle_digest="sha256:oversized-rejection-audit",
        scenario_id="oversized-rejection-audit",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:oversized-rejection-plan",
            )
        ],
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(
            bundle_repository=_BundleRepository(bundle),
            audit=audit,
        ).admit(plan)

    assert excinfo.value.code is ErrorCode.BUNDLE_MEMBER_EXECUTION_FORBIDDEN
    assert excinfo.value.details == {
        "bundle_id": oversized_bundle_id,
        "plan_id": oversized_plan_id,
    }
    assert len(audit.events) == 1
    event = audit.events[0]
    serialized = json.dumps(event.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    assert len(serialized.encode("utf-8")) <= 1024
    projections = (
        event.subject_id,
        cast(str, event.payload["plan_id"]),
        cast(str, event.payload["bundle_id"]),
    )
    assert all(len(projection) <= 200 for projection in projections)
    assert all(
        len(json.dumps(projection, ensure_ascii=True).encode("utf-8")) <= 202
        for projection in projections
    )
    assert all(projection.endswith("...[truncated]") for projection in projections)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_unicode_and_escaped_rejection_identifiers_stay_byte_bounded() -> None:
    oversized_plan_id = "plan-" + ("计划" + '"' + "\\" + "🔥") * 1000
    oversized_bundle_id = "bundle-" + ("包" + "\\" + '"' + "é☃") * 1000
    plan = _plan(oversized_plan_id)
    audit = AuditLog()
    bundle = BundleCommit(
        id=oversized_bundle_id,
        bundle_digest="sha256:unicode-escaped-rejection-audit",
        scenario_id="unicode-escaped-rejection-audit",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:unicode-escaped-rejection-plan",
            )
        ],
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(
            bundle_repository=_BundleRepository(bundle),
            audit=audit,
        ).admit(plan)

    assert excinfo.value.details == {
        "bundle_id": oversized_bundle_id,
        "plan_id": oversized_plan_id,
    }
    event = audit.events[0]
    serialized = json.dumps(event.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    assert len(serialized.encode("utf-8")) <= 1024
    projections = (
        event.subject_id,
        cast(str, event.payload["plan_id"]),
        cast(str, event.payload["bundle_id"]),
    )
    assert all(len(projection) <= 200 for projection in projections)
    assert all(
        len(json.dumps(projection, ensure_ascii=True).encode("utf-8")) <= 202
        for projection in projections
    )
    assert all(projection.endswith("...[truncated]") for projection in projections)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_credential_shaped_rejection_identifier_is_redacted_before_projection() -> None:
    actual_secret = "actual-secret"
    oversized_plan_id = f"token={actual_secret}"
    bundle_id = "neutral-bundle"
    plan = _plan(oversized_plan_id)
    audit = AuditLog()
    bundle = BundleCommit(
        id=bundle_id,
        bundle_digest="sha256:credential-shaped-rejection-audit",
        scenario_id="credential-shaped-rejection-audit",
        members=[
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest="sha256:credential-shaped-rejection-plan",
            )
        ],
    )

    with pytest.raises(DomainError) as excinfo:
        await ExecutionAdmission(
            bundle_repository=_BundleRepository(bundle),
            audit=audit,
        ).admit(plan)

    assert excinfo.value.details == {"bundle_id": bundle_id, "plan_id": oversized_plan_id}
    event = audit.events[0]
    serialized = json.dumps(event.model_dump(mode="json"), sort_keys=True, ensure_ascii=True)
    assert actual_secret not in serialized
    assert "token=actual-secret" not in serialized
    assert event.subject_id == "[REDACTED]"
    assert event.payload["plan_id"] == "[REDACTED]"
    assert len(serialized.encode("utf-8")) <= 1024
    projections = (
        event.subject_id,
        cast(str, event.payload["plan_id"]),
        cast(str, event.payload["bundle_id"]),
    )
    assert all(len(projection) <= 200 for projection in projections)
    assert all(
        len(json.dumps(projection, ensure_ascii=True).encode("utf-8")) <= 202
        for projection in projections
    )


@pytest.mark.composition
@pytest.mark.asyncio
@pytest.mark.parametrize("failed_index", [0, 1])
async def test_scheduler_fan_in_blocks_when_either_predecessor_lacks_success(
    tmp_path, failed_index: int
) -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=UTC)
    context = await _build_context(tmp_path, now=now)
    assert context.scheduler is not None
    assert context.plan_repository is not None
    assert context.bundle_commit_service is not None
    plan = await _validated_plan(
        context,
        plan_id=f"composition-fan-in-failure-{failed_index}",
        execute_at=now - timedelta(seconds=1),
    )
    predecessor_ids = ("fan-in-predecessor-a", "fan-in-predecessor-b")
    predecessor_members = [
        _dependency_member(
            predecessor_id,
            status=BundleMemberCommitStatus.EXECUTED,
            execution_status=(
                ExecutionStatus.FAILED
                if index == failed_index
                else ExecutionStatus.CONFIRMED_SUCCESS
            ),
        )
        for index, predecessor_id in enumerate(predecessor_ids)
    ]
    owner = BundleMemberCommit(
        plan_id=plan.id,
        validation_digest=plan.validation.digest if plan.validation else "missing",
        execute_at=plan.execute_at,
        status=BundleMemberCommitStatus.SCHEDULED,
        scheduled=True,
        predecessor_plan_ids=list(predecessor_ids),
    )
    bundle = BundleCommit(
        id=f"composition-fan-in-failure-bundle-{failed_index}",
        bundle_digest=f"sha256:composition-fan-in-failure-{failed_index}",
        scenario_id=f"composition-fan-in-failure-{failed_index}",
        status=BundleCommitStatus.SCHEDULED,
        members=[*predecessor_members, owner],
    )
    await context.bundle_commit_service.bundle_repository.save(bundle)
    await context.plan_repository.save_validation(plan)
    await context.scheduler.repository.schedule(plan)

    results = await context.scheduler.run_due()

    assert results == [{"plan_id": plan.id, "outcome": "dependency_failed"}]
    assert cast(SimulatedHomeAdapter, context.facade.executor.adapter).calls == []
    assert (await context.plan_repository.get(plan.id)).status is PlanStatus.READY  # type: ignore[union-attr]
    assert (await context.scheduler.repository.get(plan.id))[1] == "failed"  # type: ignore[index]
    assert await context.facade.executor.outcome_repository.list_for_plan(plan.id) == []  # type: ignore[union-attr]
    settled = await context.bundle_commit_service.bundle_repository.get(bundle.id)
    assert settled is not None
    settled_owner = next(member for member in settled.members if member.plan_id == plan.id)
    assert settled_owner.status is BundleMemberCommitStatus.DEPENDENCY_FAILED
    assert settled_owner.details["predecessor_plan_id"] == predecessor_ids[failed_index]
    assert settled_owner.details["predecessor_plan_ids"] == list(predecessor_ids)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_scheduler_fan_in_dispatches_after_both_predecessors_confirm_success(
    tmp_path,
) -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=UTC)
    context = await _build_context(tmp_path, now=now)
    assert context.scheduler is not None
    assert context.plan_repository is not None
    plan = await _validated_plan(
        context,
        plan_id="composition-fan-in-success",
        execute_at=now - timedelta(seconds=1),
    )
    predecessor_ids = ("fan-in-success-a", "fan-in-success-b")
    bundle = BundleCommit(
        id="composition-fan-in-success-bundle",
        bundle_digest="sha256:composition-fan-in-success-bundle",
        scenario_id="composition-fan-in-success",
        status=BundleCommitStatus.SCHEDULED,
        members=[
            *[
                _dependency_member(
                    predecessor_id,
                    status=BundleMemberCommitStatus.EXECUTED,
                    execution_status=ExecutionStatus.CONFIRMED_SUCCESS,
                )
                for predecessor_id in predecessor_ids
            ],
            BundleMemberCommit(
                plan_id=plan.id,
                validation_digest=plan.validation.digest if plan.validation else "missing",
                execute_at=plan.execute_at,
                status=BundleMemberCommitStatus.SCHEDULED,
                scheduled=True,
                predecessor_plan_ids=list(predecessor_ids),
            ),
        ],
    )
    assert context.bundle_commit_service is not None
    await context.bundle_commit_service.bundle_repository.save(bundle)
    await context.plan_repository.save_validation(plan)
    await context.scheduler.repository.schedule(plan)

    results = await context.scheduler.run_due()

    assert results == [{"plan_id": plan.id, "outcome": "executed"}]
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    assert len(adapter.calls) == 1
    completed = await context.plan_repository.get(plan.id)
    assert completed is not None
    assert completed.status is PlanStatus.COMPLETED
    assert (await context.scheduler.repository.get(plan.id))[1] == "executed"  # type: ignore[index]
    settled = await context.bundle_commit_service.bundle_repository.get(bundle.id)
    assert settled is not None
    assert settled.status is BundleCommitStatus.COMPLETED
    assert all(member.status is BundleMemberCommitStatus.EXECUTED for member in settled.members)
    outcomes = await context.facade.executor.outcome_repository.list_for_plan(plan.id)  # type: ignore[union-attr]
    assert len(outcomes) == 1
    assert outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS


@pytest.mark.composition
@pytest.mark.asyncio
async def test_duplicate_accepted_bundle_call_has_one_durable_and_adapter_effect(
    tmp_path,
) -> None:
    context = await _build_context(tmp_path, now=datetime(2026, 8, 30, 12, tzinfo=UTC))
    server = create_domotics_server(context)
    plan = await _validated_plan(context, plan_id="composition-duplicate-accepted")
    assert plan.validation is not None
    member = BundleCommitRequestMember(
        plan_id=plan.id,
        validation_digest=plan.validation.digest,
    )
    bundle_digest = bundle_approval_digest("composition-duplicate-scenario", [member])
    arguments = {
        "bundle_digest": bundle_digest,
        "scenario_id": "composition-duplicate-scenario",
        "members": [member.model_dump(mode="json")],
    }

    first = _structured(await server.call_tool("commit_or_schedule_bundle", arguments))
    first_bundle = await context.bundle_commit_service.bundle_repository.get(  # type: ignore[union-attr]
        first["bundle_commit_id"]
    )
    second = _structured(await server.call_tool("commit_or_schedule_bundle", arguments))

    assert first == second
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    assert len(adapter.calls) == 1
    persisted = await context.plan_repository.get(plan.id)  # type: ignore[union-attr]
    assert persisted is not None
    assert persisted.status is PlanStatus.COMPLETED
    outcomes = await context.facade.executor.outcome_repository.list_for_plan(plan.id)  # type: ignore[union-attr]
    assert len(outcomes) == 1
    attempts = await context.facade.executor.outcome_repository.list_attempts_for_plan(  # type: ignore[union-attr]
        plan.id
    )
    assert len(attempts) == 1
    assert first_bundle is not None
    second_bundle = await context.bundle_commit_service.bundle_repository.get(  # type: ignore[union-attr]
        first["bundle_commit_id"]
    )
    assert second_bundle == first_bundle
