from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.errors import InvalidTransitionError
from domoai.domain.models import Command, Plan, PlanStatus, Precondition
from domoai.persistence.repositories import (
    AuditEventRepository,
    DeviceRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
    RuntimeStateMetadataRepository,
    ScheduledPlanRepository,
    StateSnapshotRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_terminal_revalidation_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal-revalidation.sqlite3"
    first_adapter = SimulatedHomeAdapter()
    first_registry = DeviceRegistry()
    first_state_store = StateStore()
    first_audit = AuditLog()
    await DiscoveryService(
        first_adapter, first_registry, first_state_store, first_audit
    ).refresh()
    first_service = PlanService(first_registry, first_state_store, PolicyEngine([]), first_audit)
    device_id = next(device.id for device in first_registry.devices if device.type.value == "light")
    plan = first_service.validate(
        first_service.create_plan(
            "plan-terminal-revalidation-restart",
            [
                Command(
                    id="command-terminal-revalidation-restart",
                    device_id=device_id,
                    command="turn_on",
                    idempotency_key="intent-terminal-revalidation-restart",
                )
            ],
        )
    )
    first_database = SQLiteDatabase(db_path)
    await first_database.initialize()
    first_repository = PlanRepository(first_database)
    await first_repository.save_validation(plan)
    await PlanExecutor(
        first_adapter,
        first_service,
        first_audit,
        plan_repository=first_repository,
    ).execute(plan)
    assert len(first_adapter.calls) == 1
    await first_database.close()

    second_adapter = SimulatedHomeAdapter()
    second_registry = DeviceRegistry()
    second_state_store = StateStore()
    second_audit = AuditLog()
    await DiscoveryService(
        second_adapter, second_registry, second_state_store, second_audit
    ).refresh()
    second_service = PlanService(
        second_registry, second_state_store, PolicyEngine([]), second_audit
    )
    second_database = SQLiteDatabase(db_path)
    await second_database.initialize()
    second_repository = PlanRepository(second_database)
    persisted = await second_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.COMPLETED
    assert persisted.definition_digest == plan.definition_digest

    revalidated = second_service.validate(persisted)
    with pytest.raises(InvalidTransitionError):
        await second_repository.save_validation(revalidated)

    assert second_adapter.calls == []
    recovered = await second_repository.get(plan.id)
    assert recovered is not None
    assert recovered.status is PlanStatus.COMPLETED
    await second_database.close()


@pytest.mark.asyncio
async def test_scheduled_stale_precondition_after_restart_is_not_executed(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "scheduled-stale-restart.sqlite3"
    first_adapter = SimulatedHomeAdapter()
    first_registry = DeviceRegistry()
    first_state_store = StateStore()
    first_audit = AuditLog()
    await DiscoveryService(
        first_adapter, first_registry, first_state_store, first_audit
    ).refresh()
    first_service = PlanService(first_registry, first_state_store, PolicyEngine([]), first_audit)
    light_id = next(
        device.id for device in first_registry.devices if device.type.value == "light"
    )
    switch_id = next(
        device.id for device in first_registry.devices if device.type.value == "switch"
    )
    switch_state = await first_state_store.get(switch_id, "power")
    assert switch_state is not None
    execute_at = datetime.now(UTC) - timedelta(minutes=1)
    plan = first_service.validate(
        Plan(
            id="scheduled-stale-restart",
            execute_at=execute_at,
            commands=[
                Command(
                    id="scheduled-stale-restart:command",
                    device_id=light_id,
                    command="set_brightness",
                    value=60,
                    unit="%",
                    idempotency_key="scheduled-stale-restart:intent",
                    preconditions=[
                        Precondition(
                            device_id=switch_id,
                            capability="power",
                            expected=switch_state.value,
                        )
                    ],
                )
            ],
        )
    )
    first_database = SQLiteDatabase(db_path)
    await first_database.initialize()
    await ScheduledPlanRepository(first_database).schedule(plan)
    snapshot_repository = StateSnapshotRepository(first_database)
    for snapshot in await first_state_store.all():
        await snapshot_repository.save(snapshot)
    await RuntimeStateMetadataRepository(first_database).save(first_state_store.export_metadata())
    device_repository = DeviceRepository(first_database)
    for device in first_registry.devices:
        await device_repository.save(device)
    await first_database.close()

    second_database = SQLiteDatabase(db_path)
    await second_database.initialize()
    second_registry = DeviceRegistry()
    second_registry.load_persisted(await DeviceRepository(second_database).list_all())
    second_adapter = SimulatedHomeAdapter()
    second_registry.apply_snapshot(await second_adapter.discover(), second_adapter.adapter_id)
    second_state_store = StateStore()
    metadata = await RuntimeStateMetadataRepository(second_database).get()
    assert metadata is not None
    second_state_store.restore_metadata(metadata)
    second_state_store.load_persisted(await StateSnapshotRepository(second_database).list_all())
    second_audit = AuditLog()
    second_service = PlanService(
        second_registry, second_state_store, PolicyEngine([]), second_audit
    )
    second_scheduler = Scheduler(
        PlanExecutor(second_adapter, second_service, second_audit),
        ScheduledPlanRepository(second_database),
        second_audit,
    )

    result = await second_scheduler.run_due(now=execute_at)

    assert result == [{"plan_id": plan.id, "outcome": "failed"}]
    assert second_adapter.calls == []
    scheduled = await ScheduledPlanRepository(second_database).get(plan.id)
    assert scheduled is not None
    assert scheduled[1] == "failed"
    await second_database.close()


@pytest.mark.asyncio
async def test_plan_and_terminal_outcome_survive_sqlite_round_trip(tmp_path: Path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = plan_service.create_plan(
        "plan-persistence-1",
        [
            Command(
                id="command-persistence-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                idempotency_key="intent-persistence-1",
            )
        ],
    )
    validated = plan_service.validate(plan)
    database = SQLiteDatabase(tmp_path / "domoai.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    outcome_repository = ExecutionOutcomeRepository(database)
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        outcome_repository=outcome_repository,
    )

    await plan_repository.save(validated)
    summary = await executor.execute(validated)
    await database.close()

    recovered_database = SQLiteDatabase(tmp_path / "domoai.sqlite3")
    await recovered_database.initialize()
    recovered_plan = await PlanRepository(recovered_database).get(validated.id)
    recovered_outcomes = await ExecutionOutcomeRepository(recovered_database).list_for_plan(
        validated.id
    )

    assert recovered_plan is not None
    assert recovered_plan.status is PlanStatus.COMPLETED
    assert recovered_plan.execution is not None
    assert recovered_plan.execution.outcomes == summary.outcomes
    assert recovered_outcomes == summary.outcomes
    await recovered_database.close()


@pytest.mark.asyncio
async def test_persisted_audit_payload_redacts_credentials(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "audit.sqlite3")
    await database.initialize()
    repository = AuditEventRepository(database)

    await repository.append(
        event_id="audit-persistence-1",
        event_type="test",
        actor="test",
        subject_id="plan-persistence-1",
        payload={"token": "secret-value", "safe": "ok"},
        created_at="2026-08-15T00:00:00+00:00",
    )
    events = await repository.list_all()

    assert events[0].payload == {"token": "[REDACTED]", "safe": "ok"}
    await database.close()
