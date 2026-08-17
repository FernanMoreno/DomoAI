import json
from typing import Any

import httpx
import pytest

from domoai.adapters.fixtures.simulated_home import default_entities
from domoai.adapters.home_assistant.adapter import HomeAssistantAdapter
from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.domain.models import Command


class FakeHomeAssistantClient(HomeAssistantClient):
    def __init__(self) -> None:
        super().__init__("http://home-assistant.test", "fixture-token")
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fetch_states(self) -> list[dict[str, Any]]:
        return default_entities()

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        self.service_calls.append((domain, service, data))
        return []


class FailingHomeAssistantClient(FakeHomeAssistantClient):
    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        raise OSError("Home Assistant is offline")


class MultiEntityHomeAssistantClient(FakeHomeAssistantClient):
    async def fetch_states(self) -> list[dict[str, Any]]:
        return [
            {
                "entity_id": "light.multi_power",
                "domain": "light",
                "name": "Multi entity light",
                "area_id": "living_room",
                "device_id": "ha-multi-1",
                "supported_features": [],
                "state": {"power": False},
            },
            {
                "entity_id": "light.multi_brightness",
                "domain": "light",
                "name": "Multi entity light brightness",
                "area_id": "living_room",
                "device_id": "ha-multi-1",
                "supported_features": ["brightness"],
                "attributes": {"brightness_min": 0, "brightness_max": 100},
                "state": {"brightness": 40},
            },
        ]


@pytest.mark.asyncio
async def test_home_assistant_client_posts_authenticated_service_call() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"entity_id": "light.living_room_main"}])

    client = HomeAssistantClient(
        "http://home-assistant.test",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )

    result = await client.call_service(
        "light",
        "turn_on",
        {"entity_id": "light.living_room_main", "brightness_pct": 60},
    )

    assert result == [{"entity_id": "light.living_room_main"}]
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/services/light/turn_on"
    assert requests[0].headers["Authorization"] == "Bearer secret-token"
    assert json.loads(requests[0].content) == {
        "entity_id": "light.living_room_main",
        "brightness_pct": 60,
    }


@pytest.mark.asyncio
async def test_home_assistant_adapter_translates_semantic_commands() -> None:
    client = FakeHomeAssistantClient()
    adapter = HomeAssistantAdapter(client)
    await adapter.discover()

    commands = [
        Command(
            id="ha-light-on",
            device_id="living_room.living-room-main-light",
            command="turn_on",
            idempotency_key="ha-intent-light-on",
        ),
        Command(
            id="ha-light-brightness",
            device_id="living_room.living-room-main-light",
            command="set_brightness",
            value=60,
            unit="%",
            idempotency_key="ha-intent-light-brightness",
        ),
        Command(
            id="ha-cover-position",
            device_id="bedroom.bedroom-blind",
            command="set_position",
            value=40,
            unit="%",
            idempotency_key="ha-intent-cover-position",
        ),
        Command(
            id="ha-climate-temperature",
            device_id="bedroom.bedroom-climate",
            command="set_temperature",
            value=21,
            unit="°C",
            idempotency_key="ha-intent-climate-temperature",
        ),
    ]

    acknowledgements = [await adapter.execute(command) for command in commands]

    assert all(ack.accepted for ack in acknowledgements)
    assert client.service_calls == [
        ("light", "turn_on", {"entity_id": "light.living_room_main"}),
        (
            "light",
            "turn_on",
            {"entity_id": "light.living_room_main", "brightness_pct": 60},
        ),
        (
            "cover",
            "set_cover_position",
            {"entity_id": "cover.bedroom_blind", "position": 40},
        ),
        (
            "climate",
            "set_temperature",
            {"entity_id": "climate.bedroom", "temperature": 21},
        ),
    ]


@pytest.mark.asyncio
async def test_home_assistant_adapter_rejects_unsupported_and_duplicate_commands() -> None:
    client = FakeHomeAssistantClient()
    adapter = HomeAssistantAdapter(client)
    await adapter.discover()

    unsupported = await adapter.execute(
        Command(
            id="ha-unsupported",
            device_id="living_room.living-room-main-light",
            command="set_color",
            value="red",
            idempotency_key="ha-intent-unsupported",
        )
    )
    first = await adapter.execute(
        Command(
            id="ha-duplicate",
            device_id="living_room.living-room-main-light",
            command="turn_on",
            idempotency_key="ha-intent-duplicate",
        )
    )
    duplicate = await adapter.execute(
        Command(
            id="ha-duplicate-retry",
            device_id="living_room.living-room-main-light",
            command="turn_on",
            idempotency_key="ha-intent-duplicate",
        )
    )

    assert not unsupported.accepted
    assert first.accepted
    assert not duplicate.accepted
    assert len(client.service_calls) == 1


@pytest.mark.asyncio
async def test_home_assistant_adapter_routes_multi_entity_capabilities_exactly() -> None:
    client = MultiEntityHomeAssistantClient()
    adapter = HomeAssistantAdapter(client)
    await adapter.discover()

    power = await adapter.execute(
        Command(
            id="ha-multi-power",
            device_id="living_room.multi-entity-light",
            command="turn_on",
            idempotency_key="ha-multi-power-key",
        )
    )
    brightness = await adapter.execute(
        Command(
            id="ha-multi-brightness",
            device_id="living_room.multi-entity-light",
            command="set_brightness",
            value=60,
            unit="%",
            idempotency_key="ha-multi-brightness-key",
        )
    )

    assert power.accepted and brightness.accepted
    assert [call[2]["entity_id"] for call in client.service_calls] == [
        "light.multi_power",
        "light.multi_brightness",
    ]


@pytest.mark.asyncio
async def test_home_assistant_adapter_classifies_service_connectivity_failure() -> None:
    adapter = HomeAssistantAdapter(FailingHomeAssistantClient())
    await adapter.discover()

    with pytest.raises(ConnectionError, match="service call failed"):
        await adapter.execute(
            Command(
                id="ha-offline",
                device_id="living_room.living-room-main-light",
                command="turn_on",
                idempotency_key="ha-intent-offline",
            )
        )
