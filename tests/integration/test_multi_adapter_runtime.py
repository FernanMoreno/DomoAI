from __future__ import annotations

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.domain.models import AdapterSnapshot, Command, Plan
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import (
    RecordingAdapter,
    brightness_capability,
    entity,
    power_capability,
    source_snapshot,
)


@pytest.mark.asyncio
async def test_composite_runtime_aggregates_two_sources_and_routes_exact_entity() -> None:
    home_assistant = RecordingAdapter(
        "home_assistant", source_snapshot(adapter_id="home_assistant")
    )
    modbus = RecordingAdapter(
        "modbus", source_snapshot(adapter_id="modbus", include_shared_device=False)
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([home_assistant, modbus], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    result = await discovery.refresh()

    assert len(result.devices) == 3
    assert {device.id for device in result.devices} >= {
        "living_room.main_light",
        "home_assistant.environment",
        "modbus.environment",
    }
    assert result.runtime_revision == "rev-1"

    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(composite, plan_service, audit)
    facade = DomoticsFacade(plan_service, executor)
    plan = facade.validate_plan(
        Plan(
            id="route-plan",
            commands=[
                Command(
                    id="route-command",
                    device_id="living_room.main_light",
                    command="turn_on",
                    idempotency_key="route-key",
                )
            ],
        )
    )
    await facade.execute_plan(plan)

    assert [source for source, _command in home_assistant.writes] == ["light.main_power"]
    assert modbus.writes == []

    await composite.disconnect()
    assert not home_assistant.connected
    assert not modbus.connected


@pytest.mark.asyncio
async def test_composite_runtime_isolates_discovery_failure() -> None:
    healthy = RecordingAdapter("healthy", source_snapshot(adapter_id="healthy"))
    failing = RecordingAdapter("failing", source_snapshot(adapter_id="failing"), fail_discover=True)
    registry = DeviceRegistry()
    composite = CompositeAdapter([healthy, failing], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    result = await discovery.refresh()

    assert any(device.id == "healthy.environment" for device in result.devices)
    assert not any(device.id == "failing.environment" for device in result.devices)
    assert any(event.event_type == "adapter_discovery_failed" for event in audit.events)


@pytest.mark.asyncio
async def test_ambiguous_composite_route_issues_zero_writes() -> None:
    home_assistant = RecordingAdapter(
        "home_assistant", source_snapshot(adapter_id="home_assistant")
    )
    modbus = RecordingAdapter("modbus", source_snapshot(adapter_id="modbus"))
    registry = DeviceRegistry()
    composite = CompositeAdapter([home_assistant, modbus], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)
    await composite.connect()
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)

    plan = plan_service.validate(
        Plan(
            id="ambiguous-plan",
            commands=[
                Command(
                    id="ambiguous-command",
                    device_id="living_room.main_light",
                    command="set_brightness",
                    value=60,
                    idempotency_key="ambiguous-key",
                )
            ],
        )
    )

    assert plan.validation is not None
    assert plan.validation.status.value == "invalid"
    assert any(error.code == "route_ambiguous" for error in plan.validation.errors)
    assert home_assistant.writes == []
    assert modbus.writes == []


@pytest.mark.asyncio
async def test_unavailable_source_route_is_rejected_without_writes() -> None:
    adapter = RecordingAdapter("home_assistant", source_snapshot(adapter_id="home_assistant"))
    registry = DeviceRegistry()
    composite = CompositeAdapter([adapter], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()
    unavailable = source_snapshot(adapter_id="home_assistant")
    for item in unavailable.source_entities:
        if item["entity_id"] in {"light.main_power", "light.main_brightness"}:
            item["available"] = False
    adapter.snapshot = unavailable
    await discovery.refresh()

    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    plan = plan_service.validate(
        Plan(
            id="unavailable-plan",
            commands=[
                Command(
                    id="unavailable-command",
                    device_id="living_room.main_light",
                    command="turn_on",
                    idempotency_key="unavailable-key",
                )
            ],
        )
    )

    assert plan.validation is not None
    assert any(error.code == "source_unavailable" for error in plan.validation.errors)
    assert adapter.writes == []


@pytest.mark.asyncio
async def test_cross_adapter_type_conflict_is_audited_not_silently_dropped() -> None:
    first = RecordingAdapter(
        "home_assistant",
        AdapterSnapshot(
            source_entities=[
                entity(
                    entity_id="light.conflict",
                    source_device_id="conflict-device",
                    canonical_id="shared.conflict_device",
                    name="Conflict Light",
                    capabilities=[power_capability()],
                )
            ]
        ),
    )
    conflicting_entity = entity(
        entity_id="modbus.conflict",
        source_device_id="modbus-conflict-device",
        canonical_id="shared.conflict_device",
        name="Conflict Sensor",
        capabilities=[power_capability()],
    )
    conflicting_entity["semantic_type"] = "sensor"
    second = RecordingAdapter("modbus", AdapterSnapshot(source_entities=[conflicting_entity]))
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()

    conflict_events = [
        event for event in audit.events if event.event_type == "registry_identity_conflict"
    ]
    assert len(conflict_events) == 1
    assert conflict_events[0].payload["kind"] == "canonical_type_conflict"
    assert conflict_events[0].subject_id == "shared.conflict_device"
    device = registry.get("shared.conflict_device")
    assert device is not None
    assert device.type.value == "light"


@pytest.mark.asyncio
async def test_cross_adapter_capability_metadata_conflict_is_audited() -> None:
    first = RecordingAdapter(
        "home_assistant",
        AdapterSnapshot(
            source_entities=[
                entity(
                    entity_id="light.brightness_a",
                    source_device_id="brightness-conflict-device",
                    canonical_id="shared.brightness_conflict",
                    name="Brightness A",
                    capabilities=[brightness_capability()],
                )
            ]
        ),
    )
    conflicting_capability = brightness_capability()
    conflicting_capability["unit"] = "lux"
    conflicting_entity = entity(
        entity_id="modbus.brightness_b",
        source_device_id="modbus-brightness-conflict-device",
        canonical_id="shared.brightness_conflict",
        name="Brightness B",
        capabilities=[conflicting_capability],
    )
    second = RecordingAdapter("modbus", AdapterSnapshot(source_entities=[conflicting_entity]))
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()

    conflict_events = [
        event for event in audit.events if event.event_type == "registry_identity_conflict"
    ]
    assert len(conflict_events) == 1
    assert conflict_events[0].payload["kind"] == "capability_metadata_conflict"
    assert conflict_events[0].payload["capability"] == "brightness"


@pytest.mark.asyncio
async def test_no_conflict_cycle_records_zero_identity_conflict_events() -> None:
    home_assistant = RecordingAdapter(
        "home_assistant",
        source_snapshot(adapter_id="home_assistant", include_shared_device=False),
    )
    modbus = RecordingAdapter(
        "modbus", source_snapshot(adapter_id="modbus", include_shared_device=False)
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([home_assistant, modbus], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)

    await composite.connect()
    await discovery.refresh()

    assert not any(event.event_type == "registry_identity_conflict" for event in audit.events)
