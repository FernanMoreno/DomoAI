from __future__ import annotations

import time

import pytest

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import KnxMappingDocument
from domoai.adapters.knx.transport import InMemoryKnxTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, SourceRef, StateStatus
from domoai.runtime.event_consumer import RuntimeEventConsumer
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.knx import group_values, mapping_payload, updated_group_value


async def build_adapter() -> tuple[InMemoryKnxTransport, KnxAdapter]:
    transport = InMemoryKnxTransport(group_values())
    transport.write_state_map = {
        ("1/0/0", "1.001"): ("1/0/1", "1.001"),
        ("1/0/2", "5.001"): ("1/0/3", "5.001"),
        ("2/0/0", "1.001"): ("2/0/1", "1.001"),
    }
    adapter = KnxAdapter(
        transport,
        KnxMappingDocument.model_validate(mapping_payload()),
        discovery_timeout=0.05,
    )
    await adapter.connect()
    return transport, adapter


@pytest.mark.asyncio
async def test_discovery_returns_stable_entities_and_states() -> None:
    _transport, adapter = await build_adapter()

    first = await adapter.discover()
    second = await adapter.discover()

    assert first.source_entities == second.source_entities
    assert len(first.source_entities) == 5
    light = next(entity for entity in first.source_entities if entity["domain"] == "light")
    assert light["entity_id"] == "living_room.main_light"
    assert light["area_id"] == "living_room"
    assert light["available"] is True
    assert {state["capability"] for state in first.source_states} == {
        "power",
        "brightness",
        "temperature",
        "humidity",
        "occupancy",
    }


@pytest.mark.asyncio
async def test_discovery_maps_twenty_configured_entities_locally() -> None:
    transport = InMemoryKnxTransport()
    adapter = KnxAdapter(
        transport,
        KnxMappingDocument.model_validate(mapping_payload(count=20)),
    )
    await adapter.connect()

    started = time.perf_counter()
    snapshot = await adapter.discover()
    elapsed = time.perf_counter() - started

    assert len(snapshot.source_entities) == 20
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_read_state_converts_all_supported_values_and_source_refs() -> None:
    _transport, adapter = await build_adapter()
    await adapter.discover()

    states = await adapter.read_state(
        [
            SourceRef(adapter_id="knx", external_id="living_room.main_light"),
            SourceRef(adapter_id="knx", external_id="bedroom.environment"),
        ]
    )

    values = {(state.capability, state.value, state.unit) for state in states}
    assert values == {
        ("power", True, None),
        ("brightness", 50, "%"),
        ("temperature", 21.5, "°C"),
        ("humidity", 42.5, "%"),
        ("occupancy", True, None),
    }
    assert all(state.status is StateStatus.CURRENT for state in states)
    assert all(state.source_ref.adapter_id == "knx" for state in states)


@pytest.mark.asyncio
async def test_shared_state_group_address_updates_every_configured_entity() -> None:
    payload = mapping_payload()
    payload["entities"].append(
        {
            "entity_id": "office.temperature_mirror",
            "name": "Temperature Mirror",
            "area_id": "office",
            "semantic_type": "sensor",
            "capabilities": [
                {
                    "name": "temperature",
                    "dpt": "9.001",
                    "state_group_address": "2/1/1",
                }
            ],
        }
    )
    transport = InMemoryKnxTransport(group_values())
    adapter = KnxAdapter(transport, KnxMappingDocument.model_validate(payload))
    await adapter.connect()
    await adapter.discover()
    transport.incoming.clear()
    transport.enqueue(updated_group_value("2/1/1", "9.001", 22.0))

    event = await anext(adapter.subscribe_events())
    states = await adapter.read_state(
        [
            SourceRef(adapter_id="knx", external_id="bedroom.environment"),
            SourceRef(adapter_id="knx", external_id="office.temperature_mirror"),
        ]
    )

    assert event.kind == "state_changed"
    assert {state.value for state in states if state.capability == "temperature"} == {22.0}


@pytest.mark.asyncio
async def test_events_update_state_and_unknown_values_are_diagnostics() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()
    transport.incoming.clear()

    transport.enqueue(updated_group_value("1/0/3", "5.001", 75))
    state_event = await anext(adapter.subscribe_events())
    assert state_event.kind == "state_changed"

    states = await adapter.read_state(
        [SourceRef(adapter_id="knx", external_id="living_room.main_light")]
    )
    assert next(state for state in states if state.capability == "brightness").value == 75

    transport.enqueue(updated_group_value("9/9/9", "1.001", True))
    diagnostic = await anext(adapter.subscribe_events())
    assert diagnostic.kind == "adapter_diagnostic"
    assert "True" not in str(diagnostic.payload)

    transport.enqueue(updated_group_value("2/1/2", "9.007", 101))
    invalid = await anext(adapter.subscribe_events())
    assert invalid.kind == "adapter_diagnostic"
    humidity = await adapter.read_state(
        [SourceRef(adapter_id="knx", external_id="bedroom.environment")]
    )
    assert next(state for state in humidity if state.capability == "humidity").value == 42.5

    transport.enqueue(updated_group_value("2/1/2", "9.001", 42))
    mismatch = await anext(adapter.subscribe_events())
    assert mismatch.kind == "adapter_diagnostic"

    transport.set_health(False)
    unavailable = await anext(adapter.subscribe_events())
    assert unavailable.kind == "availability_changed"
    assert unavailable.payload == {"available": False}


@pytest.mark.asyncio
async def test_runtime_event_consumer_refreshes_knx_state() -> None:
    transport, adapter = await build_adapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    transport.incoming.clear()
    transport.enqueue(updated_group_value("1/0/1", "1.001", False))

    consumer = RuntimeEventConsumer(adapter, discovery, state_store, audit)
    event = await consumer.consume_once()

    device_id = registry.canonical_id_for_source("knx", "living_room.main_light")
    assert event is not None
    assert event.kind == "state_changed"
    assert device_id is not None
    state = await state_store.get(device_id, "power")
    assert state is not None
    assert state.value is False
    assert state.status is StateStatus.CURRENT


@pytest.mark.asyncio
async def test_commands_write_exact_knx_values_and_enforce_idempotency() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()

    power = await adapter.execute(
        Command(
            id="knx-command-on",
            device_id="living_room.main-light",
            command="turn_on",
            idempotency_key="knx-intent-on",
        )
    )
    brightness = await adapter.execute(
        Command(
            id="knx-command-brightness",
            device_id="living_room.main-light",
            command="set_brightness",
            value=60,
            unit="%",
            idempotency_key="knx-intent-brightness",
        )
    )
    duplicate = await adapter.execute(
        Command(
            id="knx-command-duplicate",
            device_id="living_room.main-light",
            command="turn_on",
            idempotency_key="knx-intent-on",
        )
    )

    assert power.accepted is True
    assert brightness.accepted is True
    assert duplicate.accepted is False
    assert [(write.group_address, write.dpt, write.value) for write in transport.writes] == [
        ("1/0/0", "1.001", True),
        ("1/0/2", "5.001", 60),
    ]


@pytest.mark.asyncio
async def test_invalid_unavailable_and_read_only_commands_never_write() -> None:
    transport, adapter = await build_adapter()
    await adapter.discover()

    invalid = await adapter.execute(
        Command(
            id="knx-command-invalid",
            device_id="living_room.main-light",
            command="set_brightness",
            value=101,
            unit="%",
            idempotency_key="knx-intent-invalid",
        )
    )
    unknown = await adapter.execute(
        Command(
            id="knx-command-unknown",
            device_id="unknown.device",
            command="turn_on",
            idempotency_key="knx-intent-unknown",
        )
    )
    read_only = await adapter.execute(
        Command(
            id="knx-command-read-only",
            device_id="bedroom.environment",
            command="set_temperature",
            value=22,
            unit="°C",
            idempotency_key="knx-intent-read-only",
        )
    )
    transport.set_health(False)
    unavailable = await adapter.execute(
        Command(
            id="knx-command-unavailable",
            device_id="living_room.main-light",
            command="turn_off",
            idempotency_key="knx-intent-unavailable",
        )
    )

    assert all(result.accepted is False for result in (invalid, unknown, read_only, unavailable))
    assert transport.writes == []
    health = await adapter.health()
    assert health.connected is False


@pytest.mark.asyncio
async def test_plan_executor_requires_runtime_validation_and_confirms_readback() -> None:
    transport, adapter = await build_adapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    plan = Plan(
        id="knx-plan-1",
        commands=[
            Command(
                id="knx-plan-command-1",
                device_id="living_room.main-light",
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="knx-plan-intent-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "confirmed_success"
    assert summary.outcomes[0].after_state is not None
    assert summary.outcomes[0].after_state.value == 60
    assert transport.writes[-1].value == 60
