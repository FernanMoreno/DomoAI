from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.home_assistant.config import HomeAssistantMappingConfigurationError
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.domain.models import Command, SourceRef, StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.registry import DeviceRegistry
from tests.composition.test_battery_dispatch_profile_composition import _binding
from tests.contract.test_home_assistant_provider import (
    _dispatch_provider,
    _dispatch_states,
    _ev_provider,
    _ev_states,
)
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient
from tests.fixtures.simulated_home import simulated_home_entities


@pytest.mark.asyncio
async def test_bridge_preserves_entity_routes_and_projects_provider_snapshot() -> None:
    client = FakeHomeAssistantProviderClient(simulated_home_entities())
    bridge = HomeAssistantProviderAdapter(HomeAssistantProvider(client))

    await bridge.connect()
    snapshot = await bridge.discover()

    entities = {item["entity_id"]: item for item in snapshot.source_entities}
    assert entities["light.living_room_main"]["device_id"] == "ha-light-1"
    assert entities["light.living_room_main"]["capabilities"][0]["commands"]

    states = await bridge.read_state(
        [SourceRef(adapter_id="home_assistant", external_id="light.living_room_main")]
    )
    assert [(state.capability, state.value) for state in states] == [
        ("power", False),
        ("brightness", 0),
    ]
    assert all(state.status is StateStatus.CURRENT for state in states)


@pytest.mark.asyncio
async def test_bridge_preserves_source_received_at_instead_of_rejuvenating_cache() -> None:
    observed_at = datetime(2026, 8, 25, 10, tzinfo=UTC)
    received_at = datetime(2026, 8, 25, 10, 1, tzinfo=UTC)
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    entities = simulated_home_entities()
    entities[0].update(
        {
            "last_updated": observed_at.isoformat(),
            "last_changed": observed_at.isoformat(),
            "received_at": received_at.isoformat(),
        }
    )
    client = FakeHomeAssistantProviderClient(entities)
    provider = HomeAssistantProvider(client, clock=FixedClock(now))
    bridge = HomeAssistantProviderAdapter(provider, clock=FixedClock(now))

    await bridge.connect()
    await bridge.discover()
    states = await bridge.read_state(
        [SourceRef(adapter_id="home_assistant", external_id="light.living_room_main")]
    )
    measurements = await provider.get_measurements()

    light_states = [state for state in states if state.capability == "power"]
    light_measurements = [
        item for item in measurements if item.source_ref.external_id == "light.living_room_main"
    ]
    assert light_states
    assert light_measurements
    assert light_states[0].observed_at == observed_at
    assert light_states[0].received_at == received_at
    assert light_measurements[0].observed_at == observed_at
    assert light_measurements[0].received_at == received_at


@pytest.mark.asyncio
async def test_bridge_translates_commands_and_preserves_provider_safety() -> None:
    client = FakeHomeAssistantProviderClient(simulated_home_entities())
    bridge = HomeAssistantProviderAdapter(HomeAssistantProvider(client))
    await bridge.connect()
    await bridge.discover()

    command = Command(
        id="bridge-command",
        device_id="living_room.living-room-main-light",
        command="turn_on",
        idempotency_key="bridge-intent",
    )
    first = await bridge.execute(command)
    duplicate = await bridge.execute(command)

    assert first.accepted is True
    assert first.source_ref == SourceRef(
        adapter_id="home_assistant", external_id="light.living_room_main"
    )
    assert duplicate.accepted is False
    assert len(client.service_calls) == 1
    assert client.service_calls[0][0:2] == ("light", "turn_on")


@pytest.mark.asyncio
async def test_bridge_sanitizes_provider_service_failures_as_unavailable() -> None:
    client = FakeHomeAssistantProviderClient(simulated_home_entities(), fail_services=True)
    bridge = HomeAssistantProviderAdapter(HomeAssistantProvider(client))
    await bridge.connect()
    await bridge.discover()

    with pytest.raises(ConnectionError, match="service call failed"):
        await bridge.execute(
            Command(
                id="bridge-failed-command",
                device_id="living_room.living-room-main-light",
                command="turn_on",
                idempotency_key="bridge-failed-intent",
            )
        )


@pytest.mark.asyncio
async def test_bridge_forwards_execution_context_to_provider_client() -> None:
    client = FakeHomeAssistantProviderClient(simulated_home_entities())
    bridge = HomeAssistantProviderAdapter(HomeAssistantProvider(client))
    await bridge.connect()
    await bridge.discover()

    context = ExecutionContext(
        agent_request_id="agent-ha-1",
        plan_id="plan-ha-1",
        execution_attempt_id="attempt-ha-1",
        adapter_request_id="adapter-ha-1",
    )
    await bridge.execute(
        Command(
            id="bridge-context-command",
            device_id="living_room.living-room-main-light",
            command="turn_on",
            idempotency_key="bridge-context-intent",
        ),
        context,
    )

    assert client.service_call_contexts == [context]


@pytest.mark.asyncio
async def test_bridge_takeover_matches_canonical_binding_to_static_ha_routes() -> None:
    provider, _client = _dispatch_provider(_dispatch_states())
    runtime_binding = _binding().model_copy(
        update={
            "profile": _binding().profile.model_copy(
                update={
                    "actuator": _binding().profile.actuator.model_copy(
                        update={
                            "charge_command": "open",
                            "discharge_command": "close",
                            "stop_command": "stop",
                        }
                    )
                }
            )
        }
    )
    bridge = HomeAssistantProviderAdapter(provider)
    bridge.bind_dispatchable_battery(runtime_binding)

    await bridge.connect()
    result = await bridge.acquire_control(
        ControlTakeoverRequest(
            owner="domoai",
            device_id="battery.home",
            plan_id="canonical-route-plan",
            first_command_id="canonical-route-command",
            first_command="open",
            native_scheduler_status="disabled",
            allow_native_takeover=False,
            lease_seconds=300,
        )
    )

    assert result.status is ControlLeaseStatus.ACQUIRED
    assert result.baseline is not None
    assert result.baseline.device_id == "battery.home"
    assert result.baseline.source_ref.external_id == "sensor.battery_power"


@pytest.mark.asyncio
async def test_bridge_does_not_project_battery_routes_without_runtime_binding() -> None:
    provider, _client = _dispatch_provider(_dispatch_states())
    bridge = HomeAssistantProviderAdapter(provider)

    await bridge.connect()
    snapshot = await bridge.discover()

    assert not any(
        capability.get("name") == "battery_control"
        for entity in snapshot.source_entities
        for capability in entity.get("capabilities", [])
    )


def _runtime_ev_binding() -> EVChargingBinding:
    return EVChargingBinding.model_validate(
        {
            "provider_id": "home_assistant",
            "device_id": "lab.ev_charger",
            "actuator": EVActuator(
                device_id="lab.ev_charger",
                capability="ev_charging",
                charge_command="charge_ev",
                stop_command="stop_ev",
                connected_capability="ev.connected",
                departure_capability=None,
                max_charge_kw=7.4,
            ),
            "soc_capability": "ev.soc",
            "capacity_capability": "ev.capacity",
        }
    )


@pytest.mark.asyncio
async def test_bridge_requires_explicit_ev_binding_for_command_projection() -> None:
    provider, _client = _ev_provider(_ev_states())
    bridge = HomeAssistantProviderAdapter(provider)

    await bridge.connect()
    snapshot = await bridge.discover()

    assert not any(
        capability.get("name") == "ev_charging" and capability.get("writable")
        for entity in snapshot.source_entities
        for capability in entity.get("capabilities", [])
    )


@pytest.mark.asyncio
async def test_bridge_projects_bound_ev_to_canonical_device_and_service() -> None:
    provider, client = _ev_provider(_ev_states())
    binding = _runtime_ev_binding()
    bridge = HomeAssistantProviderAdapter(provider, ev_charging_bindings=(binding,))

    await bridge.connect()
    snapshot = await bridge.discover()
    command_entity = next(
        entity
        for entity in snapshot.source_entities
        if entity["entity_id"] == "number.ev_command"
    )
    assert command_entity["canonical_id"] == "lab.ev_charger"
    assert any(
        capability["name"] == "ev_charging"
        and set(capability["commands"]) == {"charge_ev", "stop_ev"}
        for capability in command_entity["capabilities"]
    )
    assert any(
        state["capability"] == "ev.connected" and state["value"] is True
        for state in snapshot.source_states
    )

    result = await bridge.execute(
        Command(
            id="bridge-ev-command",
            device_id="lab.ev_charger",
            command="charge_ev",
            value=3.5,
            idempotency_key="bridge-ev-intent",
        )
    )

    assert result.accepted is True
    assert client.service_calls == [
        (
            "number",
            "set_value",
            {"entity_id": "number.ev_command", "value": 3.5},
        )
    ]


@pytest.mark.asyncio
async def test_registry_merges_bound_ev_readback_and_command_surfaces() -> None:
    """A writable EV route must retain the separate readable power feedback."""

    provider, _client = _ev_provider(_ev_states())
    bridge = HomeAssistantProviderAdapter(provider, ev_charging_bindings=(_runtime_ev_binding(),))

    await bridge.connect()
    snapshot = await bridge.discover()
    registry = DeviceRegistry()
    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=snapshot.source_entities,
            source_states=snapshot.source_states,
        ),
        "home_assistant",
    )

    device = registry.get("lab.ev_charger")
    assert device is not None
    capability = next(item for item in device.capabilities if item.name == "ev_charging")
    assert capability.readable is True
    assert capability.writable is True
    assert set(capability.commands) == {"charge_ev", "stop_ev"}
    routes = registry.routes_for("lab.ev_charger", "ev_charging")
    assert sum(route.readable for route in routes) == 1
    assert sum(route.writable for route in routes) == 1


@pytest.mark.asyncio
async def test_bridge_rejects_bound_ev_before_exposing_an_unavailable_route() -> None:
    provider, _client = _ev_provider(
        _ev_states(**{"binary_sensor.ev_connected": {"available": False}})
    )
    bridge = HomeAssistantProviderAdapter(
        provider, ev_charging_bindings=(_runtime_ev_binding(),)
    )

    await bridge.connect()
    with pytest.raises(HomeAssistantMappingConfigurationError, match="connected"):
        await bridge.discover()
