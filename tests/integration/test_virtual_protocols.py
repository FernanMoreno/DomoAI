from __future__ import annotations

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, ExecutionStatus, Plan
from domoai.lab.virtual_plant import VirtualHomePlant
from domoai.lab.virtual_protocols import build_virtual_protocol_adapters
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_all_protocol_projections_use_the_same_virtual_plant_state() -> None:
    plant = VirtualHomePlant.default(seed=187)
    adapters = build_virtual_protocol_adapters(plant)
    registry = DeviceRegistry()
    composite = CompositeAdapter(adapters, registry=registry)
    state_store = StateStore()
    audit = AuditLog()

    await composite.connect()
    result = await DiscoveryService(composite, registry, state_store, audit).refresh()

    assert {adapter.adapter_id for adapter in adapters} == {
        "fixture",
        "home_assistant",
        "knx",
        "modbus",
        "matter",
        "zigbee2mqtt",
    }
    assert {device.protocol for device in result.devices} == {
        "fixture",
        "home_assistant",
        "knx",
        "modbus",
        "matter",
        "zigbee2mqtt",
    }

    writable = [
        device
        for device in result.devices
        if any(cap.writable for cap in device.capabilities)
        and device.type.value not in {"energy", "ev_charger"}
        and device.id != "modbus.thermal"
    ]
    assert writable
    for index, device in enumerate(writable):
        capability = next(cap for cap in device.capabilities if cap.writable)
        command_name = capability.commands[0]
        value = (
            50 if capability.name == "brightness" else (
                21 if capability.name == "target_temperature" else None
            )
        )
        plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
        executor = PlanExecutor(composite, plan_service, audit)
        plan = plan_service.validate(
            Plan(
                id=f"virtual-protocol-{index}",
                commands=[
                    Command(
                        id=f"virtual-protocol-command-{index}",
                        device_id=device.id,
                        command=command_name,
                        value=value,
                        idempotency_key=f"virtual-protocol-key-{index}",
                    )
                ],
            )
        )
        if plan.status.value == "requires_confirmation":
            approval_store = ApprovalStore(clock=plan_service.clock)
            grant = approval_store.issue_attended_local(
                plan, operator_id="digital-twin", session_id=f"twin:{index}"
            )
            grant = approval_store.consume(grant.approval_id, plan)
            plan = plan_service.approve(plan, grant=grant)
        summary = await executor.execute(plan)
        assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS

    assert plant.write_count == len(writable)
    assert plant.invariant_violations() == []
    await composite.disconnect()
