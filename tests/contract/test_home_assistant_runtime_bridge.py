from __future__ import annotations

import pytest

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.domain.models import Command, SourceRef, StateStatus
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
