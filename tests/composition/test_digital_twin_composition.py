from __future__ import annotations

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, ExecutionStatus, Plan
from domoai.lab.qualification import DigitalTwinQualificationRunner
from domoai.lab.virtual_plant import VirtualHomePlant
from domoai.lab.virtual_protocols import build_virtual_protocol_adapters
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def _runtime() -> tuple[
    VirtualHomePlant,
    CompositeAdapter,
    DeviceRegistry,
    StateStore,
    AuditLog,
    PlanService,
    PlanExecutor,
]:
    plant = VirtualHomePlant.default(seed=187)
    registry = DeviceRegistry()
    adapter = CompositeAdapter(build_virtual_protocol_adapters(plant), registry=registry)
    state_store = StateStore(clock=plant.clock)
    audit = AuditLog()
    await adapter.connect()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=plant.clock)
    executor = PlanExecutor(adapter, plan_service, audit, clock=plant.clock)
    return plant, adapter, registry, state_store, audit, plan_service, executor


@pytest.mark.asyncio
async def test_runtime_closed_loop_reaches_virtual_readback_and_audit() -> None:
    plant, adapter, _registry, state_store, audit, plan_service, executor = await _runtime()
    plan = plan_service.validate(
        Plan(
            id="twin-composition-light",
            commands=[
                Command(
                    id="twin-composition-light-command",
                    device_id="fixture.switch",
                    command="turn_on",
                    idempotency_key="twin-composition-light-key",
                )
            ],
        )
    )

    summary = await executor.execute(plan)

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert plant.read("fixture", "fixture.switch", "power").value is True
    assert state_store.peek("fixture.switch", "power").value is True  # type: ignore[union-attr]
    assert any(event.event_type == "plan_execution_completed" for event in audit.events)
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_unavailable_virtual_source_fails_closed_after_validation() -> None:
    plant, adapter, _registry, _state_store, _audit, plan_service, executor = await _runtime()
    plan = plan_service.validate(
        Plan(
            id="twin-composition-unavailable",
            commands=[
                Command(
                    id="twin-composition-unavailable-command",
                    device_id="fixture.switch",
                    command="turn_on",
                    idempotency_key="twin-composition-unavailable-key",
                )
            ],
        )
    )
    plant.set_fault("fixture", "fixture.switch", "unavailable")

    summary = await executor.execute(plan)

    assert summary.outcomes[0].status in {
        ExecutionStatus.UNAVAILABLE,
        ExecutionStatus.FAILED,
    }
    assert plant.write_count == 0
    assert plant.read("fixture", "fixture.switch", "power").value is None
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_twin_approval_is_server_owned_and_does_not_touch_physical_adapter() -> None:
    plant, adapter, _registry, _state_store, audit, plan_service, executor = await _runtime()
    plan = plan_service.validate(
        Plan(
            id="twin-composition-approved",
            commands=[
                Command(
                    id="twin-composition-approved-command",
                    device_id="fixture.cover",
                    command="open",
                    idempotency_key="twin-composition-approved-key",
                )
            ],
        )
    )
    approval_store = ApprovalStore(clock=plant.clock)
    grant = approval_store.issue_attended_local(
        plan, operator_id="digital-twin-operator", session_id="digital-twin-session"
    )
    approved = plan_service.approve(plan, grant=approval_store.consume(grant.approval_id, plan))

    summary = await executor.execute(approved)

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert plant.read("fixture", "fixture.cover", "position").value == 100
    assert any(event.event_type == "plan_approved" for event in audit.events)
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_complete_twin_qualification_executes_cross_cutting_runtime_surfaces() -> None:
    evidence = await DigitalTwinQualificationRunner(seed=187).run()

    assert evidence.status.value == "passed"
    checks = {check.check_id: check for check in evidence.checks}
    assert checks["scheduler"].details["result"][0]["outcome"] == "executed"
    assert checks["automation"].details["evaluations"] == 1
    assert checks["optimization"].details["status"] == "optimal"
    assert checks["product"].details["next_step"] == (
        "validate_and_request_approval_before_execution"
    )
    assert checks["privacy"].details["records"] == 1
    assert checks["recovery"].details["snapshots"] > 0
    assert "plan_execution_completed" in checks["audit"].details["event_types"]
