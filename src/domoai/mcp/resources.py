"""Deterministic JSON snapshots exposed as MCP resources."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from domoai.domain.coverage import (
    AvailabilityReason,
    CoverageEvidenceRoute,
    CoverageEvidenceRow,
    CoverageEvidenceSnapshot,
    CoverageFamily,
    CoverageFamilyName,
    CoverageLimits,
    CoverageOperations,
    CoverageRowRef,
    MissingRequirement,
)
from domoai.domain.models import Area, AuthorityContext, CapabilityKind, Device, DeviceType, Policy
from domoai.optimizer.energy import EnergyContext
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.source_models import CapabilityRoute


def project_areas(
    registry: DeviceRegistry,
    *,
    visible_devices: list[Device],
    authority: AuthorityContext | None,
) -> list[Area]:
    """Project spatial visibility without modifying canonical area metadata.

    Devices must already be authorized, before optional discovery search
    filters. Empty ACL lists retain their existing unrestricted meaning.
    """
    areas = registry.areas
    if authority is None or not (authority.device_ids or authority.area_ids):
        return areas
    if authority.device_ids:
        device_areas = {device.area_id for device in visible_devices}
        areas = [area for area in areas if area.id in device_areas]
    if authority.area_ids:
        areas = [area for area in areas if area.id in authority.area_ids]
    visible_ids = {area.id for area in areas}
    return [
        area.model_copy(
            update={
                "parent_id": area.parent_id if area.parent_id in visible_ids else None,
                # External area identities do not have a verified spatial ACL.
                "source_refs": [],
            }
        )
        for area in areas
    ]


def inventory_snapshot(
    registry: DeviceRegistry,
    *,
    runtime_revision: str,
    refreshed_at: datetime | None,
    devices: list[Any] | None = None,
    areas: list[Area] | None = None,
) -> dict[str, Any]:
    selected_devices = devices if devices is not None else registry.devices
    selected_areas = areas if areas is not None else registry.areas
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "devices": [
            device.model_dump(mode="json")
            for device in sorted(selected_devices, key=lambda item: item.id)
        ],
        "areas": [
            area.model_dump(mode="json")
            for area in sorted(selected_areas, key=lambda item: item.id)
        ],
        "unsupported_sources": [],
        "refreshed_at": refreshed_at.isoformat() if refreshed_at else None,
    }


def capabilities_snapshot(
    registry: DeviceRegistry, runtime_revision: str, devices: list[Any] | None = None
) -> dict[str, Any]:
    capabilities: dict[str, dict[str, Any]] = {}
    for device in devices if devices is not None else registry.devices:
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


def _effective_route_available(
    routes: tuple[CapabilityRoute, ...], provider_ids: set[str]
) -> bool:
    """Describe current route evidence without granting execution authority."""
    return any(
        route.available and route.source_ref.adapter_id in provider_ids for route in routes
    )


def runtime_snapshot(
    registry: DeviceRegistry,
    *,
    runtime_revision: str,
    active_provider_ids: tuple[str, ...] = (),
    battery_qualification: str = "unsupported",
    devices: list[Any] | None = None,
) -> dict[str, Any]:
    """Expose the non-secret deployment matrix an agent must inspect first.

    This is deliberately derived from the canonical registry and live runtime
    composition. It describes writable routes and their source availability,
    but never turns that description into authority to execute them.
    """

    # Provider activity comes from the live runtime composition, not from
    # persisted SourceRefs. Rehydrated routes are intentionally non-executable
    # until discovery and must never make a disconnected provider look active.
    providers = {provider_id for provider_id in active_provider_ids if provider_id}
    writable: list[dict[str, Any]] = []
    for device in devices if devices is not None else registry.devices:
        for capability in device.capabilities:
            if not capability.writable:
                continue
            routes = registry.routes_for(device.id, capability.name)
            writable.append(
                {
                    "device_id": device.id,
                    "capability": capability.name,
                    "commands": sorted(capability.commands),
                    "available": _effective_route_available(routes, providers),
                    "providers": sorted({route.source_ref.adapter_id for route in routes}),
                }
            )
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "providers": [
            {"provider_id": provider_id, "active": True} for provider_id in sorted(providers)
        ],
        "writable_capabilities": writable,
        "authority": {
            "physical_execution": "plan_executor",
            "risky_mutations": "policy_and_operator_approval",
            "battery_dispatch": battery_qualification,
        },
    }


def coverage_snapshot(
    registry: DeviceRegistry,
    *,
    runtime_revision: str,
    active_provider_ids: tuple[str, ...] = (),
    devices: list[Any] | None = None,
) -> dict[str, Any]:
    """Describe semantic routes without exposing execution authority."""

    providers = {provider_id for provider_id in active_provider_ids if provider_id}
    routes: list[dict[str, Any]] = []
    for device in devices if devices is not None else registry.devices:
        for capability in device.capabilities:
            capability_routes = registry.routes_for(device.id, capability.name)
            source_ids = sorted(
                {route.source_ref.adapter_id for route in capability_routes}
            )
            routes.append(
                {
                    "device_id": device.id,
                    "capability": capability.name,
                    "protocols": source_ids,
                    "operations": {
                        "read": capability.readable,
                        "write": capability.writable,
                        "commands": sorted(capability.commands),
                    },
                    "guarantees": capability.guarantees.model_dump(mode="json"),
                    "limits": {
                        "unit": capability.unit,
                        "minimum": capability.minimum,
                        "maximum": capability.maximum,
                        "enum_values": sorted(capability.enum_values),
                    },
                    "status": "active"
                    if _effective_route_available(capability_routes, providers)
                    else "unavailable",
                    "writable": capability.writable,
                }
            )
    return {"schema_version": "v1", "runtime_revision": runtime_revision, "routes": routes}


def coverage_evidence_snapshot(
    registry: DeviceRegistry,
    *,
    runtime_revision: str,
    active_provider_ids: tuple[str, ...],
    devices: list[Device],
) -> CoverageEvidenceSnapshot:
    """Project already-authorized inventory without probing or qualifying hardware.

    Membership must be sampled by the caller once per response. A runtime
    revision labels inventory; it does not freeze operational availability.
    """
    providers = {provider for provider in active_provider_ids if provider}
    rows: list[CoverageEvidenceRow] = []
    for device in sorted(devices, key=lambda item: item.id):
        for capability in sorted(device.capabilities, key=lambda item: item.name):
            projected_routes = []
            for route in registry.routes_for(device.id, capability.name):
                identity = json.dumps(
                    [
                        device.id,
                        capability.name,
                        route.source_ref.adapter_id,
                        route.source_device_id,
                        route.source_ref.external_id,
                    ],
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                provider_active = route.source_ref.adapter_id in providers
                availability_reasons: list[AvailabilityReason] = []
                if not route.available:
                    availability_reasons.append("source_unavailable")
                if not provider_active:
                    availability_reasons.append("provider_inactive")
                projected_routes.append(
                    CoverageEvidenceRoute(
                        route_key="sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                        provider_id=route.source_ref.adapter_id,
                        source_available=route.available,
                        provider_active=provider_active,
                        effective_available=_effective_route_available((route,), providers),
                        availability_reasons=availability_reasons,
                        declared_operations=CoverageOperations(
                            read=route.readable,
                            write=route.writable,
                            commands=sorted(route.commands),
                        ),
                    )
                )
            rows.append(
                CoverageEvidenceRow(
                    device_id=device.id,
                    capability=capability.name,
                    device_type=device.type,
                    canonical_protocol_label=device.protocol,
                    declared_operations=CoverageOperations(
                        read=capability.readable,
                        write=capability.writable,
                        commands=sorted(capability.commands),
                    ),
                    declared_guarantees=capability.guarantees.model_copy(deep=True),
                    limits=CoverageLimits(
                        unit=capability.unit,
                        minimum=capability.minimum,
                        maximum=capability.maximum,
                        enum_values=sorted(capability.enum_values),
                    ),
                    status="active"
                    if any(r.effective_available for r in projected_routes)
                    else "unavailable",
                    reasons=[] if projected_routes else ["route_missing"],
                    routes=sorted(projected_routes, key=lambda item: item.route_key),
                )
            )
    return CoverageEvidenceSnapshot(
        runtime_revision=runtime_revision,
        rows=rows,
        families=_coverage_families(devices, rows),
    )


def _coverage_families(
    devices: list[Device],
    rows: list[CoverageEvidenceRow],
) -> list[CoverageFamily]:
    """Describe requested families conservatively from visible declarations only."""
    definitions: tuple[tuple[CoverageFamilyName, DeviceType | None], ...] = (
        ("lighting", DeviceType.LIGHT),
        ("metered_plugs", DeviceType.SWITCH),
        ("covers", DeviceType.COVER),
        ("hvac", DeviceType.CLIMATE),
        ("schedulable_loads", None),
    )
    families = []
    for priority, (family, device_type) in enumerate(definitions, 1):
        candidates = sorted(
            (device for device in devices if device.type == device_type),
            key=lambda item: item.id,
        )
        missing: set[MissingRequirement] = {"hardware_evidence_missing"}
        if not candidates:
            missing.add("not_present_in_visible_inventory")
        if family == "lighting":
            missing.add("requested_functions_unspecified")
        elif family == "metered_plugs":
            missing.add("plug_identity_unverified")
            if not candidates or any(
                not any(
                    cap.name in {"power", "energy", "power_consumption"}
                    and cap.readable
                    and cap.kind in {CapabilityKind.NUMBER, CapabilityKind.INTEGER}
                    and cap.unit in {"W", "kW", "Wh", "kWh"}
                    for cap in device.capabilities
                )
                for device in candidates
            ):
                missing.add("metering_missing")
        elif family in {"covers", "hvac"}:
            for device in candidates:
                capabilities = {cap.name: cap for cap in device.capabilities}
                if family == "covers":
                    position = capabilities.get("position")
                    if position is None or not position.readable:
                        missing.add("position_feedback_missing")
                    if not any(
                        cap.writable and set(cap.commands) & {"open", "close", "set_position"}
                        for cap in device.capabilities
                    ):
                        missing.add("control_missing")
                else:
                    temperature = capabilities.get("temperature")
                    if temperature is None or not temperature.readable:
                        missing.add("temperature_feedback_missing")
                    target = capabilities.get("target_temperature")
                    if target is None or not target.writable:
                        missing.add("setpoint_control_missing")
        elif family == "schedulable_loads":
            missing.update({"programmability_unverified", "resume_behavior_unverified"})
        candidate_ids = [device.id for device in candidates]
        candidate_set = set(candidate_ids)
        families.append(
            CoverageFamily(
                family=family,
                priority=priority,
                candidate_device_ids=candidate_ids,
                row_refs=[
                    CoverageRowRef(device_id=row.device_id, capability=row.capability)
                    for row in rows
                    if row.device_id in candidate_set
                ],
                missing_requirements=sorted(missing),
            )
        )
    return families


def policies_snapshot(policies: list[Policy], runtime_revision: str) -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "runtime_revision": runtime_revision,
        "policies": [
            policy.model_dump(mode="json") for policy in sorted(policies, key=lambda item: item.id)
        ],
    }


def energy_snapshot(
    registry: DeviceRegistry, runtime_revision: str, devices: list[Any] | None = None
) -> dict[str, Any]:
    available_devices = devices if devices is not None else registry.devices
    devices = [
        device
        for device in available_devices
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


def as_json(value: Any) -> str:
    payload = getattr(value, "structuredContent", value)
    if not isinstance(payload, dict):
        payload = {"error": str(payload)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
