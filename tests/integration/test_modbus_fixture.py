from __future__ import annotations

import time

import pytest

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.transport import InMemoryModbusTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, SourceRef, StateStatus
from domoai.runtime.event_consumer import RuntimeEventConsumer
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.modbus import mapping_payload, samples, updated_sample


async def build_adapter() -> tuple[InMemoryModbusTransport, ModbusAdapter]:
    transport = InMemoryModbusTransport(samples())
    transport.write_state_map = {
        (1, "coil", 1): (1, "coil", 0),
        (1, "holding_register", 11): (1, "holding_register", 10),
        (2, "coil", 3): (2, "coil", 2),
    }
    adapter = ModbusAdapter(
        transport,
        ModbusMappingDocument.model_validate(mapping_payload()),
        discovery_timeout=0.05,
        poll_interval=0,
    )
    await adapter.connect()
    return transport, adapter


@pytest.mark.asyncio
async def test_discovery_returns_stable_entities_and_maps_twenty_entities() -> None:
    transport, adapter = await build_adapter()
    first = await adapter.discover()
    second = await adapter.discover()
    assert first.source_entities == second.source_entities
    assert len(first.source_entities) == 3

    twenty = ModbusAdapter(
        InMemoryModbusTransport(),
        ModbusMappingDocument.model_validate(mapping_payload(count=20)),
        discovery_timeout=0.05,
    )
    await twenty.connect()
    started = time.perf_counter()
    snapshot = await twenty.discover()
    elapsed = time.perf_counter() - started

    assert len(snapshot.source_entities) == 20
    assert elapsed < 1.0
    assert transport.reads


@pytest.mark.asyncio
async def test_read_state_converts_values_and_preserves_current_status() -> None:
    _transport, adapter = await build_adapter()
    await adapter.discover()
    states = await adapter.read_state(
        [
            SourceRef(adapter_id="modbus", external_id="living_room.main_light"),
            SourceRef(adapter_id="modbus", external_id="bedroom.environment"),
        ]
    )

    assert {(state.capability, state.value, state.unit) for state in states} == {
        ("power", True, None),
        ("brightness", 50, "%"),
        ("temperature", 21.5, "°C"),
        ("humidity", 42.5, "%"),
        ("occupancy", True, None),
    }
    assert all(state.status is StateStatus.CURRENT for state in states)


@pytest.mark.asyncio
async def test_polling_emits_changes_diagnostics_and_availability() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()

    transport.enqueue(updated_sample(1, "coil", 0, (False,)))
    state_event = await anext(adapter.subscribe_events())
    assert state_event.kind == "state_changed"
    assert state_event.payload == {
        "entity_ids": ["living_room.main_light"],
        "capabilities": ["power"],
    }

    transport.enqueue(updated_sample(1, "input_register", 21, (1010,)))
    diagnostic_events = adapter.subscribe_events()
    diagnostic = await anext(diagnostic_events)
    while diagnostic.kind != "adapter_diagnostic":
        diagnostic = await anext(diagnostic_events)
    assert diagnostic.payload["entity_id"] == "bedroom.environment"
    assert "1010" not in str(diagnostic.payload)

    transport.set_health(False)
    unavailable = await anext(adapter.subscribe_events())
    assert unavailable.kind == "availability_changed"
    assert unavailable.payload == {"available": False}


@pytest.mark.asyncio
async def test_runtime_event_consumer_refreshes_modbus_state() -> None:
    transport, adapter = await build_adapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    transport.enqueue(updated_sample(1, "coil", 0, (False,)))

    event = await RuntimeEventConsumer(adapter, discovery, state_store, audit).consume_once()
    device_id = registry.canonical_id_for_source("modbus", "living_room.main_light")
    assert event is not None
    assert event.kind == "state_changed"
    assert device_id is not None
    state = await state_store.get(device_id, "power")
    assert state is not None
    assert state.value is False
    assert state.status is StateStatus.CURRENT


@pytest.mark.asyncio
async def test_plan_executor_validates_and_confirms_modbus_readback() -> None:
    transport, adapter = await build_adapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    plan = Plan(
        id="modbus-plan-1",
        commands=[
            Command(
                id="modbus-plan-command-1",
                device_id="living_room.main-light",
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="modbus-plan-intent-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "confirmed_success"
    assert summary.outcomes[0].after_state is not None
    assert summary.outcomes[0].after_state.value == 60
    assert transport.writes[-1].values == (60,)
