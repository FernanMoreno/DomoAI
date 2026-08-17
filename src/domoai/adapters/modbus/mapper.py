"""Project Modbus mappings and decoded values into canonical payloads."""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from domoai.adapters.modbus.codec import decode_point
from domoai.adapters.modbus.config import (
    ModbusCapabilityBinding,
    ModbusEntityConfig,
    ModbusMappingDocument,
)
from domoai.adapters.modbus.transport import ModbusSample
from domoai.domain.models import AdapterSnapshot, CapabilityKind

_UNITS: dict[str, str | None] = {
    "power": None,
    "brightness": "%",
    "temperature": "°C",
    "humidity": "%",
    "occupancy": None,
}


class ModbusMapper:
    """Pure mapping and conversion for the bounded Modbus profile."""

    def to_snapshot(
        self,
        document: ModbusMappingDocument,
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

    def entity(self, entity: ModbusEntityConfig, *, available: bool = True) -> dict[str, Any]:
        return {
            "entity_id": entity.entity_id,
            "device_id": entity.entity_id,
            "domain": entity.semantic_type,
            "name": entity.name,
            "area_id": entity.area_id or "unassigned",
            "manufacturer": entity.manufacturer,
            "model": entity.model,
            "semantic_type": entity.semantic_type,
            "unit_id": entity.unit_id,
            "capabilities": [self.capability(binding) for binding in entity.capabilities],
            "available": available,
        }

    def capability(self, binding: ModbusCapabilityBinding) -> dict[str, Any]:
        writable = binding.command is not None
        commands: list[str] = []
        if writable and binding.name == "power":
            commands = ["turn_on", "turn_off"]
        elif writable and binding.name == "brightness":
            commands = ["set_brightness"]
        kind = {
            "power": CapabilityKind.BOOLEAN.value,
            "brightness": CapabilityKind.INTEGER.value,
            "temperature": CapabilityKind.NUMBER.value,
            "humidity": CapabilityKind.NUMBER.value,
            "occupancy": CapabilityKind.BOOLEAN.value,
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

    def decode(
        self,
        entity: ModbusEntityConfig,
        binding: ModbusCapabilityBinding,
        sample: ModbusSample,
    ) -> dict[str, Any]:
        point = binding.state
        if (
            sample.unit_id != entity.unit_id
            or sample.area != point.area
            or sample.address != point.address
        ):
            raise ValueError("Modbus sample does not match configured state point")
        value = decode_point(point, sample.values)
        if binding.name in {"power", "occupancy"} and not isinstance(value, bool):
            raise ValueError(f"{binding.name} must decode to boolean")
        if binding.name in {"brightness", "humidity"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{binding.name} must decode to a number")
            if not math.isfinite(float(value)) or not 0 <= float(value) <= 100:
                raise ValueError(f"{binding.name} must be between 0 and 100")
        if binding.name == "brightness":
            if not float(value).is_integer():
                raise ValueError("brightness must decode to an integer percentage")
            value = int(value)
        return {
            "entity_id": entity.entity_id,
            "capability": binding.name,
            "value": value,
            "unit": _UNITS[binding.name],
            "available": True,
            "observed_at": sample.observed_at,
        }
