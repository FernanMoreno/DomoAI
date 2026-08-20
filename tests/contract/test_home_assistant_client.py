import json

import httpx
import pytest

from domoai.adapters.home_assistant.client import HomeAssistantClient


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
