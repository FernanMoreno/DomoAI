from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.errors import DomainError, ErrorCode, InvalidTransitionError
from domoai.domain.models import (
    Command,
    CommandPostcondition,
    Plan,
    PlanStatus,
    Precondition,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def _build_context() -> tuple[
    SimulatedHomeAdapter, DeviceRegistry, PlanService, PlanExecutor
]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    return adapter, registry, plan_service, executor


class _ContextRecordingAdapter(SimulatedHomeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.execution_contexts: list[ExecutionContext | None] = []

    async def execute(self, command, execution_context=None):
        self.execution_contexts.append(execution_context)
        return await super().execute(command, execution_context)


def _light_id(registry: DeviceRegistry) -> str:
    return next(device.id for device in registry.devices if device.type.value == "light")


def _battery_feedback_snapshot(
    value: float, *, status: StateStatus = StateStatus.CURRENT
) -> StateSnapshot:
    timestamp = datetime.now(UTC)
    return StateSnapshot(
        device_id="battery.home",
        capability="battery_power",
        value=value,
        unit="kW",
        observed_at=timestamp,
        received_at=timestamp,
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id="battery.entity"),
    )


def test_custom_command_requires_declared_postcondition_for_confirmation() -> None:
    command = Command(
        id="battery-command-custom",
        device_id="battery.home",
        command="charge_battery",
        value=2.0,
        unit="kW",
        idempotency_key="battery-command-custom",
    )

    assert not PlanExecutor._postcondition_matches(
        command, "battery_power", _battery_feedback_snapshot(2.0)
    )


def test_battery_feedback_postcondition_uses_tolerance_and_current_status() -> None:
    command = Command(
        id="battery-command-feedback",
        device_id="battery.home",
        command="charge_battery",
        value=2.0,
        unit="kW",
        idempotency_key="battery-command-feedback",
        postconditions=[
            CommandPostcondition(capability="battery_power", expected=2.0, tolerance=0.1)
        ],
    )

    assert PlanExecutor._postcondition_matches(
        command, "battery_power", _battery_feedback_snapshot(2.05)
    )
    assert not PlanExecutor._postcondition_matches(
        command, "battery_power", _battery_feedback_snapshot(2.2)
    )
    assert not PlanExecutor._postcondition_matches(
        command,
        "battery_power",
        _battery_feedback_snapshot(2.0, status=StateStatus.STALE),
    )


@pytest.mark.asyncio
async def test_single_execution_attempt_shares_one_attempt_id_across_commands() -> None:
    adapter, registry, plan_service, executor = await _build_context()
    light_id = _light_id(registry)
    plan = Plan(
        id="plan-shared-attempt",
        commands=[
            Command(
                id="cmd-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-1",
            ),
            Command(
                id="cmd-2",
                device_id=light_id,
                command="set_brightness",
                value=42,
                unit="%",
                idempotency_key="intent-2",
            ),
        ],
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    attempt_ids = {outcome.execution_attempt_id for outcome in summary.outcomes}
    assert len(attempt_ids) == 1


@pytest.mark.asyncio
async def test_terminal_plan_retry_is_rejected_without_a_second_attempt(
    tmp_path: Path,
) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    light_id = _light_id(registry)

    first_executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    plan = Plan(
        id="plan-retry",
        commands=[
            Command(
                id="cmd-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-retry-1",
            )
        ],
    )
    validated = plan_service.validate(plan)
    first_summary = await first_executor.execute(validated)
    assert first_summary.outcomes[0].execution_attempt_id is not None
    assert len(adapter.calls) == 1

    # A terminal plan cannot be reopened by revalidation. Retrying the same
    # physical intent requires a new plan identity and explicit admission.
    revalidated = plan_service.validate(validated.model_copy(update={"status": PlanStatus.READY}))
    with pytest.raises(InvalidTransitionError):
        await plan_repository.save(revalidated)

    assert len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [PlanStatus.DRAFT, PlanStatus.VALIDATED])
async def test_executor_rejects_non_executable_plan_status_before_claim(
    tmp_path: Path, status: PlanStatus
) -> None:
    adapter, registry, plan_service, _ = await _build_context()
    audit = AuditLog()
    database = SQLiteDatabase(tmp_path / f"repo-{status.value}.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    light_id = _light_id(registry)
    validated = plan_service.validate(
        Plan(
            id=f"plan-invalid-status-{status.value}",
            commands=[
                Command(
                    id="cmd-invalid-status",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key=f"intent-invalid-status-{status.value}",
                )
            ],
        )
    )
    invalid_status = validated.model_copy(update={"status": status})
    await plan_repository.save(invalid_status)

    with pytest.raises(DomainError, match="ready or approved"):
        await executor.execute(invalid_status)
    persisted = await plan_repository.get(invalid_status.id)
    assert persisted is not None and persisted.status is status


@pytest.mark.asyncio
async def test_executor_rechecks_live_command_value_before_first_write() -> None:
    adapter, registry, plan_service, executor = await _build_context()
    light_id = _light_id(registry)
    validated = plan_service.validate(
        Plan(
            id="plan-live-value-recheck",
            commands=[
                Command(
                    id="command-live-value-recheck",
                    device_id=light_id,
                    command="set_brightness",
                    value=90,
                    unit="%",
                    idempotency_key="intent-live-value-recheck",
                )
            ],
        )
    )
    mutated = validated.model_copy(
        update={"commands": [validated.commands[0].model_copy(update={"value": 101})]}
    )

    summary = await executor.execute(mutated)

    outcome = summary.outcomes[0]
    assert outcome.status.value == "rejected"
    assert outcome.error is not None
    assert outcome.error.code == ErrorCode.VALUE_OUT_OF_RANGE
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_precondition_rejection_has_attempt_id_but_no_adapter_request_id() -> None:
    adapter, registry, plan_service, executor = await _build_context()
    light_id = _light_id(registry)
    plan = Plan(
        id="plan-precondition-rejected",
        commands=[
            Command(
                id="cmd-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-precondition",
                preconditions=[Precondition(device_id=light_id, capability="power", expected=True)],
            )
        ],
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    outcome = summary.outcomes[0]
    assert outcome.execution_attempt_id
    assert outcome.adapter_request_id is None


@pytest.mark.asyncio
async def test_successful_adapter_dispatch_has_both_ids() -> None:
    adapter, registry, plan_service, executor = await _build_context()
    light_id = _light_id(registry)
    plan = Plan(
        id="plan-success",
        commands=[
            Command(
                id="cmd-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-success",
            )
        ],
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    outcome = summary.outcomes[0]
    assert outcome.execution_attempt_id
    assert outcome.adapter_request_id


@pytest.mark.asyncio
async def test_adapter_receives_the_outcome_correlation_context() -> None:
    adapter = _ContextRecordingAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    light_id = _light_id(registry)
    validated = plan_service.validate(
        Plan(
            id="plan-adapter-context",
            agent_request_id="agent-context-1",
            commands=[
                Command(
                    id="cmd-context-1",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="intent-context-1",
                )
            ],
        )
    )

    summary = await executor.execute(validated)

    outcome = summary.outcomes[0]
    context = adapter.execution_contexts[0]
    assert context == ExecutionContext(
        agent_request_id="agent-context-1",
        plan_id=validated.id,
        execution_attempt_id=outcome.execution_attempt_id,
        adapter_request_id=outcome.adapter_request_id or "",
    )
