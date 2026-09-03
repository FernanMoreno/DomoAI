"""Deterministic JSON snapshots exposed as MCP resources."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from domoai.domain.models import DeviceType, Policy
from domoai.optimizer.energy import EnergyContext
from domoai.runtime.registry import DeviceRegistry


def inventory_snapshot(
    registry: DeviceRegistry,
    *,
    runtime_revision: str,
    refreshed_at: datetime | None,
    devices: list[Any] | None = None,
) -> dict[str, Any]:
    selected_devices = devices if devices is not None else registry.devices
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "devices": [
            device.model_dump(mode="json")
            for device in sorted(selected_devices, key=lambda item: item.id)
        ],
        "areas": [
            area.model_dump(mode="json")
            for area in sorted(registry.areas, key=lambda item: item.id)
        ],
        "unsupported_sources": [],
        "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
    }


def capabilities_snapshot(registry: DeviceRegistry, runtime_revision: str) -> dict[str, Any]:
    capabilities: dict[str, dict[str, Any]] = {}
    for device in registry.devices:
        for capability in device.capabilities:
            capabilities.setdefault(
                capability.name,
                {
                    "name": capability.name,
                    "kind": capability.kind.value,
                    "units": set(),
                    "device_count": 0,
                },
            )
            if capability.unit:
                capabilities[capability.name]["units"].add(capability.unit)
            capabilities[capability.name]["device_count"] += 1
    values = []
    for entry in sorted(capabilities.values(), key=lambda item: item["name"]):
        values.append({**entry, "units": sorted(entry["units"])})
    return {"schema_version": "v1", "runtime_revision": runtime_revision, "capabilities": values}


def runtime_snapshot(
    registry: DeviceRegistry,
    *,
    runtime_revision: str,
    active_provider_ids: tuple[str, ...] = (),
    battery_qualification: str = "unsupported",
) -> dict[str, Any]:
    """Expose the non-secret deployment matrix an agent must inspect first.

    This is deliberately derived from the canonical registry and live runtime
    composition. It describes writable routes and their source availability,
    but never turns that description into authority to execute them.
    """

    # Provider activity comes from the live runtime composition, not from
    # persisted SourceRefs. Rehydrated routes are intentionally non-executable
    # until discovery and must never make a disconnected provider look active.
    providers = sorted({provider_id for provider_id in active_provider_ids if provider_id})
    writable: list[dict[str, Any]] = []
    for device in registry.devices:
        for capability in device.capabilities:
            if not capability.writable:
                continue
            routes = registry.routes_for(device.id, capability.name)
            writable.append(
                {
                    "device_id": device.id,
                    "capability": capability.name,
                    "commands": sorted(capability.commands),
                    "available": any(route.available for route in routes),
                    "providers": sorted(
                        {route.source_ref.adapter_id for route in routes}
                    ),
                }
            )
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "providers": [
            {"provider_id": provider_id, "active": True} for provider_id in providers
        ],
        "writable_capabilities": writable,
        "authority": {
            "physical_execution": "plan_executor",
            "risky_mutations": "policy_and_operator_approval",
            "battery_dispatch": battery_qualification,
        },
    }


def policies_snapshot(policies: list[Policy], runtime_revision: str) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "policies": [
            policy.model_dump(mode="json") for policy in sorted(policies, key=lambda item: item.id)
        ],
    }


def energy_snapshot(registry: DeviceRegistry, runtime_revision: str) -> dict[str, Any]:
    devices = [
        device
        for device in registry.devices
        if device.type in {DeviceType.ENERGY, DeviceType.EV_CHARGER}
        or any(
            capability.name in {"energy", "power", "power_consumption"}
            for capability in device.capabilities
        )
    ]
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "devices": [
            device.model_dump(mode="json") for device in sorted(devices, key=lambda item: item.id)
        ],
    }


def energy_context_snapshot(context: EnergyContext, runtime_revision: str) -> dict[str, Any]:
    """Serialize one complete provider result without exposing provider details."""

    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "context": context.model_dump(mode="json"),
    }


def as_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
