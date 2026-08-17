from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, PlanStatus
from domoai.persistence.repositories import (
    AuditEventRepository,
    ExecutionOutcomeRepository,
    PlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


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
