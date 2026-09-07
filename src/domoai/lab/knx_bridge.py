"""Pure translation helpers for the virtual battery KNX bridge."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from domoai.adapters.knx.config import KnxMappingDocument


@dataclass(frozen=True)
class KnxBatteryWrite:
    group_address: str
    dpt: str
    value: float


def state_to_knx_writes(
    payload: dict[str, Any], mapping: KnxMappingDocument
) -> tuple[KnxBatteryWrite, ...]:
    """Project one simulator state payload onto configured KNX state groups."""

    required = {
        "soc_kwh": payload.get("soc_kwh"),
        "power_kw": payload.get("power_kw"),
        "capacity_kwh": payload.get("capacity_kwh"),
    }
    if any(
        not isinstance(value, (int, float)) or isinstance(value, bool)
        for value in required.values()
    ):
        raise ValueError("battery state requires numeric soc_kwh, power_kw and capacity_kwh")
    soc = cast(float | int, required["soc_kwh"])
    power = cast(float | int, required["power_kw"])
    capacity = cast(float | int, required["capacity_kwh"])
    values = {
        "battery.soc": float(soc),
        "battery.power": float(power),
        "battery.capacity": float(capacity),
    }
    entity = next(
        (item for item in mapping.entities if item.semantic_type == "energy"),
        None,
    )
    if entity is None:
        raise ValueError("KNX mapping has no energy entity")
    return tuple(
        KnxBatteryWrite(binding.state_group_address, binding.dpt, values[binding.name])
        for binding in entity.capabilities
        if binding.name in values
    )


def knx_command_to_mqtt(value: object) -> bytes:
    """Encode a signed KNX kW setpoint for the simulator MQTT command topic."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("KNX battery power command must be numeric")
    return f"{float(value):.6g}".encode("ascii")
