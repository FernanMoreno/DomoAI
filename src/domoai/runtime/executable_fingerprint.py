"""Canonical fingerprints for the executable device inventory."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from domoai.domain.models import Capability, Device
from domoai.runtime.source_models import CapabilityRoute


def capability_fingerprint(
    device: Device,
    capability: Capability,
    routes: Sequence[CapabilityRoute],
) -> str:
    """Hash the executable contract used by one command dependency."""

    payload = {
        "device": _device_payload(device),
        "capability": _capability_payload(capability),
        "routes": _sorted_payloads(_route_payload(route) for route in routes),
    }
    return _digest(payload)


def inventory_fingerprint(
    devices: Sequence[Device],
    routes_by_capability: Mapping[tuple[str, str], Sequence[CapabilityRoute]],
) -> str:
    """Hash all executable device, capability, and route semantics."""

    entries: list[dict[str, Any]] = []
    for device in sorted(devices, key=lambda item: item.id):
        capabilities = [
            {
                "capability": _capability_payload(capability),
                "routes": _sorted_payloads(
                    _route_payload(route)
                    for route in routes_by_capability.get((device.id, capability.name), ())
                ),
            }
            for capability in sorted(device.capabilities, key=lambda item: item.name)
        ]
        entries.append({"device": _device_payload(device), "capabilities": capabilities})
    return _digest(entries)


def _device_payload(device: Device) -> dict[str, Any]:
    """Return only device fields that affect policy or physical execution."""

    return {
        "id": device.id,
        "type": device.type.value,
        "area_id": device.area_id,
        "availability": device.availability.value,
        "protocol": device.protocol,
    }


def _capability_payload(capability: Capability) -> dict[str, Any]:
    return {
        "name": capability.name,
        "kind": capability.kind.value,
        "unit": capability.unit,
        "readable": capability.readable,
        "writable": capability.writable,
        "minimum": capability.minimum,
        "maximum": capability.maximum,
        "enum_values": sorted(capability.enum_values),
        "commands": sorted(capability.commands),
        "constraints": capability.constraints,
    }


def _route_payload(route: CapabilityRoute) -> dict[str, Any]:
    return {
        "canonical_device_id": route.canonical_device_id,
        "capability": route.capability,
        "source_device_id": route.source_device_id,
        "local_canonical_id": route.local_canonical_id,
        "commands": sorted(route.commands),
        "available": route.available,
        "source_ref": route.source_ref.model_dump(mode="json"),
    }


def _sorted_payloads(payloads: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(payloads, key=_canonical_json)


def _digest(payload: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(payload).encode()).hexdigest()}"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
