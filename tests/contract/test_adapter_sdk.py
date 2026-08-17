from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.sdk import AdapterManifest, AdapterRegistration, AdapterRegistry
from domoai.domain.models import CapabilityKind, DeviceType


def manifest() -> AdapterManifest:
    return AdapterManifest(
        adapter_id="fixture",
        name="Fixture adapter",
        contract_version="v1",
        protocol="fixture",
        package_name="domoai-tests",
        package_version="0.1.0",
        device_types=list(DeviceType),
        capabilities=[
            {
                "name": "power",
                "kind": CapabilityKind.BOOLEAN,
                "readable": True,
                "writable": True,
                "commands": ["turn_on", "turn_off", "toggle"],
            },
            {
                "name": "brightness",
                "kind": CapabilityKind.INTEGER,
                "unit": "%",
                "readable": True,
                "writable": True,
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_brightness"],
            },
            {
                "name": "occupancy",
                "kind": CapabilityKind.BOOLEAN,
                "readable": True,
                "writable": False,
                "optional": True,
            },
        ],
    )


@pytest.mark.asyncio
async def test_capability_negotiation_reports_supported_and_optional() -> None:
    adapter = SimulatedHomeAdapter()
    snapshot = await adapter.discover()
    registry = AdapterRegistry()
    registry.register(AdapterRegistration(manifest=manifest(), factory=SimulatedHomeAdapter))

    report = registry.compatibility("fixture", snapshot)

    statuses = {item.name: item.status for item in report.capabilities}
    assert statuses["power"] == "supported"
    assert statuses["brightness"] == "supported"
    assert statuses["occupancy"] == "optional"
    assert report.status == "compatible"


@pytest.mark.asyncio
async def test_capability_negotiation_marks_missing_required_capability_degraded() -> None:
    adapter = SimulatedHomeAdapter()
    snapshot = await adapter.discover()
    registry = AdapterRegistry()
    registry.register(AdapterRegistration(manifest=manifest(), factory=SimulatedHomeAdapter))
    snapshot.source_entities = [
        entity
        for entity in snapshot.source_entities
        if not any(
            capability.get("name") == "brightness"
            for capability in entity.get("capabilities", [])
            if isinstance(capability, dict)
        )
    ]

    report = registry.compatibility("fixture", snapshot)

    brightness = next(item for item in report.capabilities if item.name == "brightness")
    assert brightness.status == "unsupported"
    assert report.status == "degraded"


def test_manifest_round_trip_is_strict_and_versioned() -> None:
    restored = AdapterManifest.model_validate(manifest().model_dump())

    assert restored == manifest()
    assert restored.schema_version == "v1"
    assert restored.contract_version == "v1"

    with pytest.raises(ValidationError):
        AdapterManifest.model_validate({**manifest().model_dump(), "unexpected": True})


def test_writable_capability_requires_commands() -> None:
    payload = manifest().model_dump()
    payload["capabilities"][0]["commands"] = []

    with pytest.raises(ValidationError, match="commands"):
        AdapterManifest.model_validate(payload)
