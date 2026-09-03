"""Project KNX mappings and DPT values into canonical adapter payloads."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from domoai.adapters.knx.config import KnxCapabilityBinding, KnxEntityConfig, KnxMappingDocument
from domoai.domain.models import AdapterSnapshot, CapabilityKind

_UNITS: dict[str, str | None] = {
    "power": None,
    "brightness": "%",
    "temperature": "°C",
    "humidity": "%",
    "occupancy": None,
    "battery.soc": "kWh",
    "battery.power": "kW",
    "battery.capacity": "kWh",
}


class KnxMapper:
    """Pure mapping and DPT conversion for the bounded KNX profile."""

    def to_snapshot(
        self,
        document: KnxMappingDocument,
        *,
        states: Iterable[dict[str, Any]] = (),
        available: bool = True,
    ) -> AdapterSnapshot:
        return AdapterSnapshot(
            source_entities=[
                self.entity(entity, available=available) for entity in document.entities
            ],
            source_states=list(states),
        )

    def entity(self, entity: KnxEntityConfig, *, available: bool = True) -> dict[str, Any]:
        capabilities = [self.capability(binding) for binding in entity.capabilities]
        return {
            "entity_id": entity.entity_id,
            "device_id": entity.entity_id,
            "domain": entity.semantic_type,
            "name": entity.name,
            "area_id": entity.area_id or "unassigned",
            "manufacturer": entity.manufacturer,
            "model": entity.model,
            "semantic_type": entity.semantic_type,
            "capabilities": capabilities,
            "available": available,
        }

    def capability(self, binding: KnxCapabilityBinding) -> dict[str, Any]:
        writable = binding.command_group_address is not None
        commands: list[str] = []
        if writable and binding.name == "power":
            commands = ["turn_on", "turn_off"]
        elif writable and binding.name == "brightness":
            commands = ["set_brightness"]
        elif writable and binding.name == "battery.power":
            commands = ["charge_battery", "discharge_battery", "stop_battery"]
        kind = {
            "power": CapabilityKind.BOOLEAN.value,
            "brightness": CapabilityKind.INTEGER.value,
            "temperature": CapabilityKind.NUMBER.value,
            "humidity": CapabilityKind.NUMBER.value,
            "occupancy": CapabilityKind.BOOLEAN.value,
            "battery.soc": CapabilityKind.NUMBER.value,
            "battery.power": CapabilityKind.NUMBER.value,
            "battery.capacity": CapabilityKind.NUMBER.value,
        }[binding.name]
        result: dict[str, Any] = {
            "name": binding.name,
            "kind": kind,
            "unit": _UNITS[binding.name],
            "readable": True,
            "writable": writable,
            "commands": commands,
        }
        if binding.name in {"brightness", "humidity"}:
            result.update({"minimum": 0, "maximum": 100})
        return result

    def decode(self, document: KnxMappingDocument, value: Any) -> dict[str, Any]:
        decoded = self.decode_many(document, value)
        if not decoded:
            raise ValueError(f"unknown KNX group address: {value.group_address}")
        return decoded[0]

    def decode_many(self, document: KnxMappingDocument, value: Any) -> list[dict[str, Any]]:
        bindings = [
            (entity, binding)
            for entity in document.entities
            for binding in entity.capabilities
            if binding.state_group_address == value.group_address
        ]
        if not bindings:
            raise ValueError(f"unknown KNX group address: {value.group_address}")
        if any(binding.dpt != value.dpt for _entity, binding in bindings):
            raise ValueError(f"DPT mismatch for KNX group address: {value.group_address}")
        return [
            {
                "entity_id": entity.entity_id,
                "capability": binding.name,
                "value": self._decode_value(binding, value.value),
                "unit": _UNITS[binding.name],
                "available": True,
                "observed_at": value.observed_at,
            }
            for entity, binding in bindings
        ]

    @staticmethod
    def _decode_value(binding: KnxCapabilityBinding, value: object) -> bool | int | float:
        if binding.name in {"power", "occupancy"}:
            if not isinstance(value, bool):
                raise ValueError(f"{binding.name} must be boolean")
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{binding.name} must be numeric")
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise ValueError(f"{binding.name} must be finite")
        if binding.name in {"brightness", "humidity"} and not 0 <= numeric_value <= 100:
            raise ValueError(f"{binding.name} must be between 0 and 100")
        if binding.name == "brightness":
            return round(numeric_value)
        if binding.name in {"battery.soc", "battery.capacity"} and numeric_value < 0:
            raise ValueError(f"{binding.name} must not be negative")
        return value
