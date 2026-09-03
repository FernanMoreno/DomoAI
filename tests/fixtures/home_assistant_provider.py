"""Deterministic Home Assistant client fixtures for Provider SDK tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from copy import deepcopy
from typing import Any

from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.runtime.execution_context import ExecutionContext


class FakeHomeAssistantProviderClient(HomeAssistantClient):
    def __init__(
        self,
        states: Iterable[dict[str, Any]],
        *,
        registry: Iterable[dict[str, Any]] = (),
        device_registry: Iterable[dict[str, Any]] = (),
        events: Iterable[dict[str, Any]] = (),
        fail_services: bool = False,
        healthy: bool = True,
    ) -> None:
        super().__init__("http://home-assistant.test", "fixture-token")
        self.states = [deepcopy(state) for state in states]
        self.registry = [deepcopy(entry) for entry in registry]
        self.device_registry = [deepcopy(entry) for entry in device_registry]
        self.events = [deepcopy(event) for event in events]
        self.fail_services = fail_services
        self.healthy = healthy
        self.fetch_states_calls = 0
        self.service_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.service_call_contexts: list[ExecutionContext | None] = []

    async def fetch_states(self) -> list[dict[str, Any]]:
        self.fetch_states_calls += 1
        return deepcopy(self.states)

    async def fetch_entity_registry(self) -> list[dict[str, Any]]:
        return deepcopy(self.registry)

    async def fetch_device_registry(self) -> list[dict[str, Any]]:
        return deepcopy(self.device_registry)

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        execution_context: ExecutionContext | None = None,
    ) -> list[dict[str, Any]]:
        if self.fail_services:
            raise OSError("fixture service unavailable; token=should-not-leak")
        self.service_calls.append((domain, service, deepcopy(data)))
        self.service_call_contexts.append(execution_context)
        entity_id = data.get("entity_id")
        for state in self.states:
            if state.get("entity_id") != entity_id:
                continue
            if isinstance(state.get("state"), dict):
                if service == "turn_on":
                    state["state"]["power"] = True
                elif service == "turn_off":
                    state["state"]["power"] = False
                elif service == "toggle":
                    state["state"]["power"] = not bool(state["state"].get("power"))
            elif service in {"turn_on", "turn_off"}:
                state["state"] = "on" if service == "turn_on" else "off"
        return []

    async def health(self) -> bool:
        return self.healthy

    async def subscribe_state_events(self) -> AsyncIterator[dict[str, Any]]:
        for event in self.events:
            yield deepcopy(event)
