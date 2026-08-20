from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, ExecutionStatus, Plan, PlanStatus
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.replay import PlanReplayer
from domoai.runtime.state_store import StateStore


async def _build_live_context(tmp_path):
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    return adapter, registry, state_store, plan_service, executor, plan_repository


async def _persist_and_execute_plan(tmp_path, device_id: str, plan_id: str = "replay-plan-1"):
    (
        adapter,
        registry,
        state_store,
        plan_service,
        executor,
        plan_repository,
    ) = await _build_live_context(tmp_path)
    plan = Plan(
        id=plan_id,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id=device_id,
                command="turn_on",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )
    validated = plan_service.validate(plan)
    await executor.execute(validated)
    return adapter, registry, state_store, plan_repository


@pytest.mark.asyncio
async def test_replay_reproduces_a_previously_executed_plan(tmp_path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")

    _, _, _, plan_repository = await _persist_and_execute_plan(tmp_path, device_id)
    replayer = PlanReplayer(plan_repository)

    result = await replayer.replay("replay-plan-1")

    assert result.found is True
    assert result.incomplete_reconstruction_notes == []
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert result.status is PlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_replay_uses_the_plans_own_time_not_real_now(tmp_path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")

    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    far_future = datetime(2099, 1, 1, tzinfo=UTC)
    plan = Plan(
        id="replay-future-1",
        execute_at=far_future,
        commands=[
            Command(
                id="replay-future-1:command",
                device_id=device_id,
                command="turn_on",
                idempotency_key="replay-future-1:intent",
            )
        ],
    )
    await plan_repository.save(plan)
    replayer = PlanReplayer(plan_repository)

    result = await replayer.replay("replay-future-1")

    assert result.found is True
    assert result.status is PlanStatus.COMPLETED
    assert result.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS


@pytest.mark.asyncio
async def test_replay_is_repeatable(tmp_path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")

    _, _, _, plan_repository = await _persist_and_execute_plan(
        tmp_path, device_id, plan_id="replay-repeatable-1"
    )
    replayer = PlanReplayer(plan_repository)

    first = await replayer.replay("replay-repeatable-1")
    second = await replayer.replay("replay-repeatable-1")

    assert first.status == second.status
    assert first.original_status == second.original_status
    assert [outcome.status for outcome in first.outcomes] == [
        outcome.status for outcome in second.outcomes
    ]
    assert first.incomplete_reconstruction_notes == second.incomplete_reconstruction_notes
    assert [outcome.completed_at for outcome in first.outcomes] == [
        outcome.completed_at for outcome in second.outcomes
    ]


@pytest.mark.asyncio
async def test_replay_reports_not_found_for_unknown_plan_id(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    replayer = PlanReplayer(plan_repository)

    result = await replayer.replay("does-not-exist")

    assert result.found is False
    assert result.status is None
    assert result.original_status is None
    assert result.outcomes == []
    assert result.incomplete_reconstruction_notes == []


@pytest.mark.asyncio
async def test_replay_reports_incomplete_reconstruction_when_a_device_is_missing(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    plan = Plan(
        id="replay-missing-device-1",
        commands=[
            Command(
                id="replay-missing-device-1:command",
                device_id="switch.does_not_exist",
                command="turn_on",
                idempotency_key="replay-missing-device-1:intent",
            )
        ],
    )
    await plan_repository.save(plan)
    replayer = PlanReplayer(plan_repository)

    result = await replayer.replay("replay-missing-device-1")

    assert result.found is True
    assert len(result.incomplete_reconstruction_notes) == 1
    assert "switch.does_not_exist" in result.incomplete_reconstruction_notes[0]


@pytest.mark.asyncio
async def test_replay_never_touches_the_live_adapter(tmp_path) -> None:
    device_id_holder = {}

    async def build_and_execute_live():
        adapter = SimulatedHomeAdapter()
        registry = DeviceRegistry()
        state_store = StateStore()
        audit = AuditLog()
        await DiscoveryService(adapter, registry, state_store, audit).refresh()
        device_id = next(device.id for device in registry.devices if device.type.value == "switch")
        device_id_holder["id"] = device_id
        plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
        database = SQLiteDatabase(tmp_path / "repo.sqlite3")
        await database.initialize()
        plan_repository = PlanRepository(database)
        executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
        plan = Plan(
            id="replay-isolation-1",
            commands=[
                Command(
                    id="replay-isolation-1:command",
                    device_id=device_id,
                    command="turn_on",
                    idempotency_key="replay-isolation-1:intent",
                )
            ],
        )
        validated = plan_service.validate(plan)
        await executor.execute(validated)
        return adapter, plan_repository

    live_adapter, plan_repository = await build_and_execute_live()
    calls_before_replay = len(live_adapter.calls)

    replayer = PlanReplayer(plan_repository)
    result = await replayer.replay("replay-isolation-1")

    assert result.found is True
    assert len(live_adapter.calls) == calls_before_replay


@pytest.mark.asyncio
async def test_replay_never_mutates_live_state_store(tmp_path) -> None:
    discovery_adapter = SimulatedHomeAdapter()
    discovery_registry = DeviceRegistry()
    discovery_state_store = StateStore()
    await DiscoveryService(
        discovery_adapter, discovery_registry, discovery_state_store, AuditLog()
    ).refresh()
    device_id = next(
        device.id for device in discovery_registry.devices if device.type.value == "switch"
    )

    _, _, live_state_store, plan_repository = await _persist_and_execute_plan(
        tmp_path, device_id, plan_id="replay-state-isolation-1"
    )
    live_snapshots_before = await live_state_store.all()

    replayer = PlanReplayer(plan_repository)
    await replayer.replay("replay-state-isolation-1")

    live_snapshots_after = await live_state_store.all()
    assert live_snapshots_after == live_snapshots_before


@pytest.mark.asyncio
async def test_replay_never_persists_anything(tmp_path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")

    _, _, _, plan_repository = await _persist_and_execute_plan(
        tmp_path, device_id, plan_id="replay-no-persist-1"
    )
    original = await plan_repository.get("replay-no-persist-1")

    replayer = PlanReplayer(plan_repository)
    await replayer.replay("replay-no-persist-1")

    after = await plan_repository.get("replay-no-persist-1")
    assert after == original
