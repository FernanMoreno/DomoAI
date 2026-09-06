from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import (
    ControlLeaseStatus,
    PhysicalBaseline,
    Plan,
    SourceRef,
    TakeoverResult,
)
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.control_takeover import ControlTakeoverPort
from domoai.runtime.events import AuditLog
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class _LeaseController(ControlTakeoverPort):
    def __init__(self, *, release_result: bool = True) -> None:
        self.release_result = release_result
        self.release_calls = 0
        self.emergency_stop_calls = 0

    async def acquire_for_plan(self, *, plan_id: str, commands):
        now = datetime.now(UTC)
        command = commands[0]
        baseline = PhysicalBaseline(
            device_id=command.device_id,
            capability="power",
            power_kw=0.0,
            observed_at=now,
            received_at=now,
            source_ref=SourceRef(adapter_id="fixture", external_id=command.device_id),
            state_revision="state-1",
            native_scheduler_status="disabled",
        )
        return TakeoverResult(
            lease_id=f"lease:{plan_id}",
            status=ControlLeaseStatus.ACQUIRED,
            owner="runtime",
            device_id=command.device_id,
            plan_id=plan_id,
            acquired_at=now,
            expires_at=now + timedelta(minutes=1),
            baseline=baseline,
            first_command_id=command.id,
            evidence_digest="sha256:lease",
        )

    async def assert_still_owned(self, *, plan_id: str) -> bool:
        return True

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        self.emergency_stop_calls += 1
        return True

    async def release_for_plan(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        self.release_calls += 1
        return self.release_result


class _ExplodingAdapter(SimulatedHomeAdapter):
    async def execute(self, command, execution_context=None):
        raise RuntimeError("adapter exploded after lease acquisition")


async def _build_plan_executor(
    tmp_path,
    adapter: SimulatedHomeAdapter,
    controller,
    operational_metrics: RuntimeOperationalMetrics | None = None,
):
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "authority.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = plan_service.validate(
        Plan(
            id="authority-plan",
            commands=[
                {
                    "id": "authority-command",
                    "device_id": device_id,
                    "command": "turn_on",
                    "idempotency_key": "authority-intent",
                }
            ],
        )
    )
    await plan_repository.save_validation(plan)
    return PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        control_takeover=controller,
        operational_metrics=operational_metrics,
    ), plan, plan_repository, audit


@pytest.mark.asyncio
async def test_lease_is_released_and_plan_becomes_unknown_after_execution_exception(
    tmp_path,
) -> None:
    controller = _LeaseController()
    metrics = RuntimeOperationalMetrics()
    executor, plan, repository, _audit = await _build_plan_executor(
        tmp_path, _ExplodingAdapter(), controller, metrics
    )

    with pytest.raises(RuntimeError, match="adapter exploded"):
        await executor.execute(plan)

    assert controller.release_calls == 1
    persisted = await repository.get(plan.id)
    assert persisted is not None
    assert persisted.status.value == "unknown"
    assert metrics.snapshot()["leases"] == {
        "acquire_total": 1,
        "expired_total": 0,
        "released_total": 1,
        "release_failed_total": 0,
        "unknown_total": 0,
    }


@pytest.mark.asyncio
async def test_unconfirmed_release_marks_successful_physical_attempt_unknown(tmp_path) -> None:
    controller = _LeaseController(release_result=False)
    metrics = RuntimeOperationalMetrics()
    executor, plan, repository, audit = await _build_plan_executor(
        tmp_path, SimulatedHomeAdapter(), controller, metrics
    )

    summary = await executor.execute(plan)

    assert summary.outcomes
    assert controller.release_calls == 1
    persisted = await repository.get(plan.id)
    assert persisted is not None
    assert persisted.status.value == "unknown"
    assert any(event.event_type == "unknown_authority" for event in audit.events)
    assert metrics.snapshot()["leases"]["release_failed_total"] == 1
    assert metrics.snapshot()["leases"]["unknown_total"] == 1
