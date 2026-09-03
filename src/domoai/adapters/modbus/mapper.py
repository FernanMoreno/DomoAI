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
    "battery.soc": "kWh",
    "battery.power": "kW",
    "battery.capacity": "kWh",
    "ev.soc": "kWh",
    "ev_charging": "kW",
    "ev.capacity": "kWh",
    "ev.connected": None,
    "water.flow_rate": "L/min",
    "water.total_volume": "L",
    "thermal.indoor_temperature": "°C",
    "thermal.hvac_power": "kW",
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
        elif writable and binding.name == "battery.power":
            commands = ["charge_battery", "discharge_battery", "stop_battery"]
        elif writable and binding.name == "ev_charging":
            commands = ["charge_ev", "stop_ev"]
        elif writable and binding.name == "thermal.hvac_power":
            commands = ["heat_thermostat", "cool_thermostat", "stop_thermostat"]
        kind = {
            "power": CapabilityKind.BOOLEAN.value,
            "brightness": CapabilityKind.INTEGER.value,
            "temperature": CapabilityKind.NUMBER.value,
            "humidity": CapabilityKind.NUMBER.value,
            "occupancy": CapabilityKind.BOOLEAN.value,
            "battery.soc": CapabilityKind.NUMBER.value,
            "battery.power": CapabilityKind.NUMBER.value,
            "battery.capacity": CapabilityKind.NUMBER.value,
            "ev.soc": CapabilityKind.NUMBER.value,
            "ev_charging": CapabilityKind.NUMBER.value,
            "ev.capacity": CapabilityKind.NUMBER.value,
            "ev.connected": CapabilityKind.BOOLEAN.value,
            "water.flow_rate": CapabilityKind.NUMBER.value,
            "water.total_volume": CapabilityKind.NUMBER.value,
            "thermal.indoor_temperature": CapabilityKind.NUMBER.value,
            "thermal.hvac_power": CapabilityKind.NUMBER.value,
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
        if binding.name in {"power", "occupancy", "ev.connected"} and not isinstance(value, bool):
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
        if binding.name in {
            "battery.soc",
            "battery.power",
            "battery.capacity",
            "ev.soc",
            "ev_charging",
            "ev.capacity",
            "water.flow_rate",
            "water.total_volume",
            "thermal.indoor_temperature",
            "thermal.hvac_power",
        }:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{binding.name} must decode to a number")
            if not math.isfinite(float(value)):
                raise ValueError(f"{binding.name} must be finite")
        return {
            "entity_id": entity.entity_id,
            "capability": binding.name,
            "value": value,
            "unit": _UNITS[binding.name],
            "available": True,
            "observed_at": sample.observed_at,
        }
