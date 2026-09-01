"""Authenticated Home Assistant HTTP and WebSocket client."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

from domoai.runtime.execution_context import ExecutionContext


class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    @property
    def websocket_url(self) -> str:
        scheme = "wss" if self.base_url.startswith("https://") else "ws"
        host = self.base_url.removeprefix("https://").removeprefix("http://")
        return f"{scheme}://{host}/api/websocket"

    async def fetch_states(self) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, transport=self.transport
                ) as client:
                    response = await client.get(
                        f"{self.base_url}/api/states",
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, list):
                        raise ValueError("Home Assistant /api/states returned a non-list payload")
                    return [dict(item) for item in payload]
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
                if attempt == 0:
                    await asyncio.sleep(0)
        assert last_error is not None
        raise last_error

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        execution_context: ExecutionContext | None = None,
    ) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {self.token}"}
        if execution_context is not None:
            headers.update(
                {
                    "X-DomoAI-Plan-ID": execution_context.plan_id,
                    "X-DomoAI-Execution-Attempt-ID": execution_context.execution_attempt_id,
                    "X-DomoAI-Adapter-Request-ID": execution_context.adapter_request_id,
                }
            )
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/api/services/{domain}/{service}",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Home Assistant service call returned a non-list payload")
        return [dict(item) for item in payload if isinstance(item, dict)]

    async def fetch_entity_registry(self) -> list[dict[str, Any]]:
        """Fetch enabled entity-registry entries through the HA WebSocket API."""

        result = await self._fetch_websocket_result("config/entity_registry/list")
        entries = result.get("entities", []) if isinstance(result, dict) else result
        if not isinstance(entries, list):
            raise ValueError("Home Assistant entity registry returned an invalid payload")
        return [self._decode_entity_registry_entry(dict(entry)) for entry in entries]

    async def fetch_device_registry(self) -> list[dict[str, Any]]:
        """Fetch device identity claims through the HA WebSocket API."""

        result = await self._fetch_websocket_result("config/device_registry/list")
        entries = result if isinstance(result, list) else []
        return [self._decode_device_registry_entry(dict(entry)) for entry in entries]

    async def _fetch_websocket_result(self, command: str) -> Any:
        """Authenticate once and return one Home Assistant WebSocket result."""

        async with websockets.connect(self.websocket_url, open_timeout=self.timeout) as socket:
            await self._authenticate_socket(socket)
            await socket.send(
                json.dumps(
                    {
                        "id": 1,
                        "type": command,
                    }
                )
            )
            while True:
                payload = json.loads(await socket.recv())
                if payload.get("id") != 1:
                    continue
                if not payload.get("success"):
                    raise ValueError(f"Home Assistant WebSocket command failed: {command}")
                return payload.get("result", {})

    async def health(self) -> bool:
        try:
            await self.fetch_states()
        except (httpx.HTTPError, ValueError):
            return False
        return True

    async def subscribe_state_events(self) -> AsyncIterator[dict[str, Any]]:
        """Stream state changes and reconnect once after a dropped socket."""

        reconnects = 0
        while True:
            try:
                async with websockets.connect(
                    self.websocket_url, open_timeout=self.timeout
                ) as socket:
                    await self._authenticate_socket(socket)
                    await socket.send(
                        json.dumps(
                            {
                                "id": 1,
                                "type": "subscribe_events",
                                "event_type": "state_changed",
                            }
                        )
                    )
                    async for raw_message in socket:
                        message = json.loads(raw_message)
                        if message.get("type") == "event":
                            yield message
                return
            except (OSError, ValueError, PermissionError, websockets.WebSocketException):
                if reconnects >= 1:
                    raise
                reconnects += 1
                await asyncio.sleep(0)

    async def _authenticate_socket(self, socket: Any) -> None:
        auth_required = json.loads(await socket.recv())
        if auth_required.get("type") != "auth_required":
            raise ValueError("Home Assistant WebSocket did not request authentication")
        await socket.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth_result = json.loads(await socket.recv())
        if auth_result.get("type") != "auth_ok":
            raise PermissionError("Home Assistant WebSocket authentication failed")

    @staticmethod
    def _decode_entity_registry_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Expand HA's compact display-registry keys to provider-friendly keys."""

        return {
            "entity_id": entry.get("entity_id", entry.get("ei")),
            "platform": entry.get("platform", entry.get("pl")),
            "area_id": entry.get("area_id", entry.get("ai")),
            "device_id": entry.get("device_id", entry.get("di")),
            "name": entry.get("name", entry.get("en")),
            "unique_id": entry.get("unique_id", entry.get("ui")),
        }

    @staticmethod
    def _decode_device_registry_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Expand a device registry entry into stable identity claims."""

        return {
            "id": entry.get("id"),
            "identifiers": entry.get("identifiers", []),
            "connections": entry.get("connections", []),
            "manufacturer": entry.get("manufacturer"),
            "model": entry.get("model"),
            "name": entry.get("name"),
        }
