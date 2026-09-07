from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.domain.models import Command, SourceRef, StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.execution_context import ExecutionContext
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
