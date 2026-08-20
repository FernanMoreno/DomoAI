from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, PlanStatus, Precondition
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
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


def _light_id(registry: DeviceRegistry) -> str:
    return next(device.id for device in registry.devices if device.type.value == "light")


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
async def test_two_separate_execute_calls_produce_distinct_attempt_ids(tmp_path: Path) -> None:
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
    first_attempt_id = first_summary.outcomes[0].execution_attempt_id

    # Revalidate against current runtime state and reset to a claimable
    # status, simulating a legitimate retry of the same plan.
    revalidated = plan_service.validate(validated.model_copy(update={"status": PlanStatus.READY}))
    await plan_repository.save(revalidated)
    second_executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    second_summary = await second_executor.execute(revalidated)
    second_attempt_id = second_summary.outcomes[0].execution_attempt_id

    assert first_attempt_id != second_attempt_id


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
