"""Map the bounded Zigbee2MQTT v1 profile into canonical adapter payloads."""

from __future__ import annotations

import math
import re
from typing import Any

from domoai.domain.models import CapabilityKind, DeviceType


def canonical_id(friendly_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", friendly_name.lower()).strip("-")
    return f"unassigned.{normalized or 'device'}"


def flatten_exposes(exposes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for expose in exposes:
        flattened.append(expose)
        features = expose.get("features")
        if isinstance(features, list):
            flattened.extend(
                item for item in features if isinstance(item, dict)
            )
    return flattened


def map_definition(definition: dict[str, Any], *, available: bool = True) -> dict[str, Any]:
    friendly_name = str(definition.get("friendly_name") or "")
    raw_definition = definition.get("definition")
    device_definition = raw_definition if isinstance(raw_definition, dict) else {}
    exposes_raw = device_definition.get("exposes", [])
    exposes = flatten_exposes(exposes_raw) if isinstance(exposes_raw, list) else []
    expose_types = {str(item.get("type")) for item in exposes}
    properties = {str(item.get("property")) for item in exposes}

    if not definition.get("supported", False):
        semantic_type = DeviceType.UNSUPPORTED
        capabilities: list[dict[str, Any]] = []
        domain = "unsupported"
    elif "light" in expose_types:
        semantic_type = DeviceType.LIGHT
        domain = "light"
        capabilities = _power_capability()
        if "brightness" in properties:
            capabilities.append(_brightness_capability())
    elif "switch" in expose_types or "state" in properties:
        semantic_type = DeviceType.SWITCH
        domain = "switch"
        capabilities = _power_capability()
    elif properties.intersection({"temperature", "humidity", "occupancy"}):
        semantic_type = DeviceType.SENSOR
        domain = "sensor"
        capabilities = _sensor_capabilities(exposes)
    else:
        semantic_type = DeviceType.UNSUPPORTED
        domain = "unsupported"
        capabilities = []

    return {
        "entity_id": friendly_name,
        "device_id": str(definition.get("ieee_address") or friendly_name),
        "domain": domain,
        "name": friendly_name,
        "area_id": "unassigned",
        "manufacturer": device_definition.get("vendor"),
        "model": device_definition.get("model") or definition.get("model_id"),
        "semantic_type": semantic_type.value,
        "capabilities": capabilities,
        "available": available and not bool(definition.get("disabled", False)),
    }


def map_states(
    friendly_name: str,
    payload: dict[str, Any],
    *,
    available: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    states: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    if "state" in payload:
        value = payload["state"]
        if value == "ON":
            states.append(_state(friendly_name, "power", True, None, available))
        elif value == "OFF":
            states.append(_state(friendly_name, "power", False, None, available))
        else:
            diagnostics.append("state must be ON or OFF")
    if "brightness" in payload:
        raw = payload["brightness"]
        if _number_in_range(raw, 0, 254):
            states.append(
                _state(
                    friendly_name,
                    "brightness",
                    round(float(raw) * 100 / 254),
                    "%",
                    available,
                )
            )
        else:
            diagnostics.append("brightness must be a number between 0 and 254")
    for name, unit, minimum, maximum in (
        ("temperature", "°C", None, None),
        ("humidity", "%", 0, 100),
    ):
        if name in payload:
            value = payload[name]
            if _number_in_range(value, minimum, maximum):
                states.append(_state(friendly_name, name, value, unit, available))
            else:
                diagnostics.append(f"{name} is not a valid numeric value")
    if "occupancy" in payload:
        value = payload["occupancy"]
        if isinstance(value, bool):
            states.append(_state(friendly_name, "occupancy", value, None, available))
        else:
            diagnostics.append("occupancy must be boolean")
    return states, diagnostics


def _power_capability() -> list[dict[str, Any]]:
    return [
        {
            "name": "power",
            "kind": CapabilityKind.BOOLEAN.value,
            "readable": True,
            "writable": True,
            "commands": ["turn_on", "turn_off", "toggle"],
        }
    ]


def _brightness_capability() -> dict[str, Any]:
    return {
        "name": "brightness",
        "kind": CapabilityKind.INTEGER.value,
        "unit": "%",
        "readable": True,
        "writable": True,
        "minimum": 0,
        "maximum": 100,
        "commands": ["set_brightness"],
    }


def _sensor_capabilities(exposes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    for item in exposes:
        property_name = str(item.get("property") or "")
        if property_name == "temperature":
            capabilities.append(_sensor_capability("temperature", "°C"))
        elif property_name == "humidity":
            capabilities.append(_sensor_capability("humidity", "%"))
        elif property_name == "occupancy":
            capabilities.append(
                {
                    "name": "occupancy",
                    "kind": CapabilityKind.BOOLEAN.value,
                    "readable": True,
                    "writable": False,
                }
            )
    return capabilities


def _sensor_capability(name: str, unit: str) -> dict[str, Any]:
    return {
        "name": name,
        "kind": CapabilityKind.NUMBER.value,
        "unit": unit,
        "readable": True,
        "writable": False,
    }


def _state(
    friendly_name: str,
    capability: str,
    value: Any,
    unit: str | None,
    available: bool,
) -> dict[str, Any]:
    return {
        "entity_id": friendly_name,
        "capability": capability,
        "value": value,
        "unit": unit,
        "available": available,
    }


def _number_in_range(value: Any, minimum: float | None, maximum: float | None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)):
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)
