"""Deterministic adapter used by contract and integration tests."""

from __future__ import annotations

import re
import time as _time
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
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
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext


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

    def __init__(
        self, entities: list[dict[str, Any]] | None = None, *, clock: Clock | None = None
    ) -> None:
        self._entities = deepcopy(entities if entities is not None else default_entities())
        self._clock = clock or SystemClock()
        self._events: list[SourceEvent] = []
        self._connected = False
        self.calls: list[Command] = []
        self._executed_idempotency_keys: set[str] = set()
        # Spec 165 FR-009: climate.bedroom is the first fixture device whose
        # temperature genuinely evolves over time rather than being a
        # static dict. Deliberately NOT importing domoai.lab.thermal_simulator
        # here: import-linter's "Composition-root layers must depend only
        # downward" contract forbids domoai.adapters -> domoai.lab (caught by
        # the architecture-contract composition test, not assumed) -- lab is
        # a higher-level composition/test-harness layer built on top of
        # adapters, not the reverse. This is a small, independent
        # implementation of the same physical recurrence (research.md
        # Decision 1's linear RC model), not a reuse of the lab module.
        self._climate_hvac_mode: dict[str, str] = {}
        self._climate_last_tick_by_entity: dict[str, float] = {}

    def _sync_climate_state(self, entity: dict[str, Any]) -> None:
        if entity["domain"] != "climate":
            return
        entity_id = entity["entity_id"]
        state = entity.setdefault("state", {})
        elapsed = min(max(_time.monotonic() - self._climate_last_tick(entity_id), 0.0), 10.0)
        mode = self._climate_hvac_mode.get(entity_id, "off")
        if elapsed and mode != "off":
            current = float(state["temperature"])
            # Same linear RC recurrence the CP-SAT optimizer reasons about
            # (research.md Decision 1), evaluated directly in floating
            # point for this fixture: dT = dt*(heat*COP - cool*COP)/C.
            # No passive UA-loss term here -- deliberately simplified for
            # the fixture (which never declares an exterior temperature),
            # keeping the "unchanged unless commanded" baseline exact.
            capacitance_kwh_per_c = 0.5
            heat_kw = 2.0 if mode == "heat" else 0.0
            cool_kw = 2.0 if mode == "cool" else 0.0
            heating_cop = 3.0
            cooling_cop = 2.5
            dt_hours = elapsed / 3600
            delta_c = (
                dt_hours
                * (heat_kw * heating_cop - cool_kw * cooling_cop)
                / capacitance_kwh_per_c
            )
            updated = current + delta_c
            target = state.get("target_temperature")
            # Simple bang-bang thermostat control, matching what a real
            # thermostat does: stop heating/cooling once the target is
            # reached, rather than overshooting forever.
            if target is not None:
                if mode == "heat" and updated >= float(target):
                    updated = float(target)
                    self._climate_hvac_mode[entity_id] = "off"
                elif mode == "cool" and updated <= float(target):
                    updated = float(target)
                    self._climate_hvac_mode[entity_id] = "off"
            state["temperature"] = updated

    def _climate_last_tick(self, entity_id: str) -> float:
        last = self._climate_last_tick_by_entity.get(entity_id)
        now = _time.monotonic()
        self._climate_last_tick_by_entity[entity_id] = now
        return last if last is not None else now

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def discover(self) -> AdapterSnapshot:
        for entity in self._entities:
            self._sync_climate_state(entity)
        return HomeAssistantMapper().to_snapshot(self._entities)

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {source_ref.external_id for source_ref in source_refs}
        for entity in self._entities:
            if entity["entity_id"] in wanted:
                self._sync_climate_state(entity)
        snapshot = HomeAssistantMapper().to_snapshot(
            [entity for entity in self._entities if entity["entity_id"] in wanted]
        )
        now = self._clock.now()
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

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        entity = self._find_for_device(command.device_id)
        return await self.execute_source(command, entity["entity_id"], execution_context)

    async def execute_source(
        self,
        command: Command,
        source_entity_id: str,
        execution_context: ExecutionContext | None = None,
    ) -> AdapterExecutionAck:
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

    def _apply_command(self, entity: dict[str, Any], command: str, value: Any) -> None:
        state = entity.setdefault("state", {})
        if command == "set_temperature":
            self._sync_climate_state(entity)
            state["target_temperature"] = value
            current = float(state["temperature"])
            target = float(value)
            entity_id = entity["entity_id"]
            if target > current:
                self._climate_hvac_mode[entity_id] = "heat"
            elif target < current:
                self._climate_hvac_mode[entity_id] = "cool"
            else:
                self._climate_hvac_mode[entity_id] = "off"
        elif command == "turn_on":
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
