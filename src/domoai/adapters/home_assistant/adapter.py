"""Home Assistant adapter using the client and semantic mapper."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from domoai.adapters.home_assistant.client import HomeAssistantClient
from domoai.adapters.home_assistant.mapper import HomeAssistantMapper
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    Command,
    SourceEvent,
    SourceRef,
    StateSnapshot,
    StateStatus,
)


class HomeAssistantAdapter:
    adapter_id = "home_assistant"

    def __init__(self, client: HomeAssistantClient) -> None:
        self.client = client
        self.mapper = HomeAssistantMapper()
        self._connected = False
        self._source_by_device: dict[str, str] = {}
        self._source_by_command: dict[tuple[str, str], list[str]] = {}
        self._canonical_by_source: dict[str, str] = {}
        self._executed_idempotency_keys: set[str] = set()

    async def connect(self) -> None:
        if not await self.client.health():
            raise ConnectionError("Home Assistant is unavailable")
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        snapshot = self.mapper.to_snapshot(await self.client.fetch_states())
        self._remember_sources(snapshot)
        return snapshot

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {source_ref.external_id for source_ref in source_refs}
        snapshot = self.mapper.to_snapshot(await self.client.fetch_states())
        now = datetime.now(UTC)
        return [
            StateSnapshot(
                device_id=state["entity_id"],
                capability=state["capability"],
                value=state.get("value"),
                unit=state.get("unit"),
                observed_at=now,
                received_at=now,
                status=(
                    StateStatus.CURRENT
                    if state.get("available", True)
                    else StateStatus.UNAVAILABLE
                ),
                source_ref=SourceRef(
                    adapter_id=self.adapter_id,
                    external_id=state["entity_id"],
                ),
            )
            for state in snapshot.source_states
            if state["entity_id"] in wanted
        ]

    async def execute(self, command: Command) -> AdapterExecutionAck:
        candidates = self._source_by_command.get((command.device_id, command.command), [])
        if len(candidates) != 1:
            message = "Ambiguous Home Assistant capability route" if len(candidates) > 1 else (
                f"Unknown Home Assistant command route: {command.command}"
            )
            return AdapterExecutionAck(accepted=False, message=message)
        return await self.execute_source(command, candidates[0])

    async def execute_source(
        self, command: Command, source_entity_id: str
    ) -> AdapterExecutionAck:
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(accepted=False, message="Duplicate idempotency key")
        canonical_id = self._canonical_by_source.get(source_entity_id)
        if canonical_id is None:
            return AdapterExecutionAck(
                accepted=False, message=f"Unknown Home Assistant entity: {source_entity_id}"
            )
        translated = self._translate_command(source_entity_id, command)
        if translated is None:
            return AdapterExecutionAck(
                accepted=False,
                message=f"Unsupported Home Assistant command: {command.command}",
            )
        domain, service, data = translated
        try:
            await self.client.call_service(domain, service, data)
        except (ConnectionError, OSError, ValueError) as error:
            raise ConnectionError(f"Home Assistant service call failed: {error}") from error
        self._executed_idempotency_keys.add(command.idempotency_key)
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id=source_entity_id),
            message="Home Assistant service call accepted",
        )

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        async for event in self.client.subscribe_state_events():
            yield SourceEvent(kind="state_changed", payload=event)

    async def health(self) -> AdapterHealth:
        connected = self._connected and await self.client.health()
        return AdapterHealth(adapter_id=self.adapter_id, connected=connected)

    def _remember_sources(self, snapshot: AdapterSnapshot) -> None:
        self._source_by_device.clear()
        self._source_by_command.clear()
        self._canonical_by_source.clear()
        canonical_by_source_device: dict[str, str] = {}
        used_ids: set[str] = set()
        for entity in snapshot.source_entities:
            entity_id = str(entity["entity_id"])
            source_device_id = str(entity.get("device_id") or entity_id)
            canonical_id = canonical_by_source_device.get(source_device_id)
            if canonical_id is None:
                explicit_canonical_id = entity.get("canonical_id")
                if explicit_canonical_id is not None:
                    canonical_id = str(explicit_canonical_id)
                else:
                    area_id = str(entity.get("area_id") or "unassigned")
                    name = str(entity.get("name") or entity_id)
                    base = f"{area_id}.{_slug(name)}"
                    canonical_id = base
                    suffix = 2
                    while canonical_id in used_ids:
                        canonical_id = f"{base}-{suffix}"
                        suffix += 1
                canonical_by_source_device[source_device_id] = canonical_id
                used_ids.add(canonical_id)
            self._source_by_device[canonical_id] = entity_id
            self._canonical_by_source[entity_id] = canonical_id
            for capability in entity.get("capabilities", []):
                for command in capability.get("commands", []):
                    self._source_by_command.setdefault((canonical_id, str(command)), []).append(
                        entity_id
                    )

    @staticmethod
    def _translate_command(
        entity_id: str, command: Command
    ) -> tuple[str, str, dict[str, Any]] | None:
        domain = entity_id.split(".", 1)[0]
        data: dict[str, Any] = {"entity_id": entity_id}
        if domain in {"light", "switch"} and command.command in {
            "turn_on",
            "turn_off",
            "toggle",
        }:
            return domain, command.command, data
        if domain == "light" and command.command == "set_brightness":
            data["brightness_pct"] = command.value
            return domain, "turn_on", data
        if domain == "cover":
            cover_services = {
                "open": "open_cover",
                "close": "close_cover",
                "stop": "stop_cover",
            }
            if command.command in cover_services:
                return domain, cover_services[command.command], data
            if command.command == "set_position":
                data["position"] = command.value
                return domain, "set_cover_position", data
        if domain == "climate" and command.command == "set_temperature":
            data["temperature"] = command.value
            return domain, "set_temperature", data
        return None


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "device"
