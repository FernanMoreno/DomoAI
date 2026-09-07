from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import websockets

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


@pytest.mark.asyncio
async def test_home_assistant_client_reuses_and_closes_one_http_lifecycle_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[])

    client = HomeAssistantClient(
        "http://home-assistant.test",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )

    await client.fetch_states()
    first_http_client = client.http_client
    await client.fetch_states()

    assert first_http_client is client.http_client
    assert first_http_client is not None and not first_http_client.is_closed
    await client.close()
    await client.close()
    assert first_http_client.is_closed
    assert len(requests) == 2


class _HangingWebSocket:
    def __init__(self) -> None:
        self._receives = 0

    async def __aenter__(self) -> _HangingWebSocket:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send(self, message: str) -> None:
        del message

    async def recv(self) -> str:
        self._receives += 1
        if self._receives == 1:
            return json.dumps({"type": "auth_required"})
        await asyncio.sleep(0.05)
        if self._receives == 2:
            return json.dumps({"type": "auth_ok"})
        return json.dumps({"id": 1, "success": True, "result": {}})


class _IdleEventWebSocket:
    def __init__(self) -> None:
        self.receives = 0
        self.entries = 0
        self._idle = asyncio.Event()

    async def __aenter__(self) -> _IdleEventWebSocket:
        self.entries += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def send(self, message: str) -> None:
        del message

    async def recv(self) -> str:
        self.receives += 1
        if self.receives % 3 == 1:
            return json.dumps({"type": "auth_required"})
        if self.receives % 3 == 2:
            return json.dumps({"type": "auth_ok"})
        await self._idle.wait()
        return "{}"


@pytest.mark.asyncio
async def test_home_assistant_websocket_auth_has_an_operation_deadline(monkeypatch) -> None:
    socket = _HangingWebSocket()
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: socket)
    client = HomeAssistantClient("http://home-assistant.test", "secret-token", timeout=0.001)

    with pytest.raises(asyncio.TimeoutError):
        await client._fetch_websocket_result("config/entity_registry/list")


@pytest.mark.asyncio
async def test_home_assistant_event_stream_does_not_reconnect_when_idle(monkeypatch) -> None:
    socket = _IdleEventWebSocket()
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: socket)
    client = HomeAssistantClient("http://home-assistant.test", "secret-token", timeout=0.01)

    events = client.subscribe_state_events()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(anext(events), timeout=0.05)

    assert socket.entries == 1
    await events.aclose()
