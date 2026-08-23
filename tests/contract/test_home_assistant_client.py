import json

import httpx
import pytest

from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.runtime.execution_context import ExecutionContext


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
async def test_home_assistant_client_adds_non_secret_correlation_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        "http://home-assistant.test",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    context = ExecutionContext(
        agent_request_id="agent-http-1",
        plan_id="plan-http-1",
        execution_attempt_id="attempt-http-1",
        adapter_request_id="adapter-http-1",
    )

    await client.call_service(
        "light", "turn_on", {"entity_id": "light.living_room_main"}, execution_context=context
    )

    assert requests[0].headers["X-DomoAI-Plan-ID"] == "plan-http-1"
    assert requests[0].headers["X-DomoAI-Execution-Attempt-ID"] == "attempt-http-1"
    assert requests[0].headers["X-DomoAI-Adapter-Request-ID"] == "adapter-http-1"
