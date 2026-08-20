"""Deterministic adapter used by contract and integration tests."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from domoai.adapters.home_assistant.mapper import HomeAssistantMapper
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterHealth,
    AdapterSnapshot,
    AvailabilityChangedEvent,
    Command,
    MetadataChangedEvent,
    SourceEvent,
    SourceRef,
    StateSnapshot,
    StateStatus,
)


def default_entities() -> list[dict[str, Any]]:
    entities = [
        {
            "entity_id": "light.living_room_main",
            "domain": "light",
            "name": "Living room main light",
            "area_id": "living_room",
            "device_id": "ha-light-1",
            "manufacturer": "Fixture Lamps",
            "model": "L100",
            "supported_features": ["brightness"],
            "attributes": {"brightness_min": 0, "brightness_max": 100},
            "state": {"power": False, "brightness": 0},
        },
        {
            "entity_id": "switch.garden_pump",
            "domain": "switch",
            "name": "Garden pump",
            "area_id": "garden",
            "device_id": "ha-switch-1",
            "manufacturer": "Fixture Controls",
            "model": "S100",
            "supported_features": [],
            "attributes": {},
            "state": {"power": False},
        },
        {
            "entity_id": "cover.bedroom_blind",
            "domain": "cover",
            "name": "Bedroom blind",
            "area_id": "bedroom",
            "device_id": "ha-cover-1",
            "manufacturer": "Fixture Covers",
            "model": "C100",
            "supported_features": ["position", "open", "close", "stop"],
            "attributes": {"position_min": 0, "position_max": 100},
            "state": {"position": 50},
        },
        {
            "entity_id": "climate.bedroom",
            "domain": "climate",
            "name": "Bedroom climate",
            "area_id": "bedroom",
            "device_id": "ha-climate-1",
            "manufacturer": "Fixture Climate",
            "model": "T100",
            "supported_features": ["target_temperature"],
            "attributes": {"temperature_min": 16, "temperature_max": 27, "unit": "°C"},
            "state": {"temperature": 20, "target_temperature": 21},
        },
        {
            "entity_id": "sensor.living_room_temperature",
            "domain": "sensor",
            "name": "Living room temperature",
            "area_id": "living_room",
            "device_id": "ha-sensor-1",
            "manufacturer": "Fixture Sensors",
            "model": "E100",
            "supported_features": [],
            "attributes": {"unit": "°C", "measurement": "temperature"},
            "state": {"temperature": 20.5},
        },
        {
            "entity_id": "sensor.house_power",
            "domain": "sensor",
            "name": "House power",
            "area_id": "garage",
            "device_id": "ha-energy-1",
            "manufacturer": "Fixture Energy",
            "model": "P100",
            "supported_features": [],
            "attributes": {"unit": "W", "measurement": "power"},
            "state": {"power": 420},
        },
    ]
    for entity in entities:
        source_device_id = str(entity["device_id"])
        entity["identity_keys"] = [f"fixture:device:{source_device_id}"]
        entity["connections"] = [f"fixture:{source_device_id}"]
    return entities


class SimulatedHomeAdapter:
    adapter_id = "fixture"

    def __init__(self, entities: list[dict[str, Any]] | None = None) -> None:
        self._entities = deepcopy(entities if entities is not None else default_entities())
        self._events: list[SourceEvent] = []
        self._connected = False
        self.calls: list[Command] = []
        self._executed_idempotency_keys: set[str] = set()

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        return HomeAssistantMapper().to_snapshot(self._entities)

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {source_ref.external_id for source_ref in source_refs}
        snapshot = HomeAssistantMapper().to_snapshot(
            [entity for entity in self._entities if entity["entity_id"] in wanted]
        )
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
                    StateStatus.CURRENT if state.get("available", True) else StateStatus.UNAVAILABLE
                ),
                source_ref=SourceRef(
                    adapter_id=self.adapter_id,
                    external_id=state["entity_id"],
                ),
            )
            for state in snapshot.source_states
        ]

    async def execute(self, command: Command) -> AdapterExecutionAck:
        entity = self._find_for_device(command.device_id)
        return await self.execute_source(command, entity["entity_id"])

    async def execute_source(self, command: Command, source_entity_id: str) -> AdapterExecutionAck:
        if command.idempotency_key in self._executed_idempotency_keys:
            return AdapterExecutionAck(accepted=False, message="Duplicate idempotency key")
        try:
            entity = self._find(source_entity_id)
        except KeyError:
            return AdapterExecutionAck(accepted=False, message="Unknown fixture entity")
        self._executed_idempotency_keys.add(command.idempotency_key)
        self._apply_command(entity, command.command, command.value)
        self.calls.append(command)
        return AdapterExecutionAck(accepted=True, message="Fixture command accepted")

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        while self._events:
            yield self._events.pop(0)

    async def health(self) -> AdapterHealth:
        return AdapterHealth(adapter_id=self.adapter_id, connected=self._connected)

    def set_available(self, entity_id: str, available: bool) -> None:
        entity = self._find(entity_id)
        entity["available"] = available
        self._events.append(
            AvailabilityChangedEvent(
                payload={"entity_id": entity_id, "available": available},
            )
        )

    def rename(self, entity_id: str, name: str) -> None:
        entity = self._find(entity_id)
        entity["name"] = name
        self._events.append(MetadataChangedEvent(payload={"entity_id": entity_id, "name": name}))

    def _find(self, entity_id: str) -> dict[str, Any]:
        for entity in self._entities:
            if entity["entity_id"] == entity_id:
                return entity
        raise KeyError(entity_id)

    def _find_for_device(self, device_id: str) -> dict[str, Any]:
        for entity in self._entities:
            area_id = str(entity.get("area_id") or "unassigned")
            name = str(entity.get("name") or entity["entity_id"])
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "device"
            if f"{area_id}.{slug}" == device_id:
                return entity
        raise KeyError(device_id)

    @staticmethod
    def _apply_command(entity: dict[str, Any], command: str, value: Any) -> None:
        state = entity.setdefault("state", {})
        if command == "turn_on":
            state["power"] = True
        elif command == "turn_off":
            state["power"] = False
        elif command == "toggle":
            state["power"] = not bool(state.get("power", False))
        elif command == "set_brightness":
            state["brightness"] = value
        elif command == "set_position":
            state["position"] = value
        elif command == "open":
            state["position"] = 100
        elif command == "close":
            state["position"] = 0
