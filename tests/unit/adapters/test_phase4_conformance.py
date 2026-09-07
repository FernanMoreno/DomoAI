import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.sdk import AdapterManifest, AdapterRegistration, AdapterRegistry
from domoai.domain.models import CapabilityKind, DeviceType


def _manifest() -> AdapterManifest:
    return AdapterManifest(
        adapter_id="fixture",
        name="Fixture adapter",
        protocol="fixture",
        package_name="domoai-tests",
        package_version="0.1.0",
        device_types=list(DeviceType),
        capabilities=[
            {
                "name": "brightness",
                "kind": CapabilityKind.INTEGER,
                "unit": "%",
                "readable": True,
                "writable": True,
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_brightness"],
            }
        ],
    )


@pytest.mark.asyncio
async def test_provider_with_matching_observed_guarantees_is_compatible() -> None:
    adapter = SimulatedHomeAdapter()
    snapshot = await adapter.discover()
    registry = AdapterRegistry()
    registry.register(AdapterRegistration(manifest=_manifest(), factory=SimulatedHomeAdapter))

    report = registry.compatibility("fixture", snapshot)

    assert report.status.value == "compatible"
    assert not report.diagnostics


@pytest.mark.asyncio
async def test_provider_overstating_observed_numeric_range_is_degraded() -> None:
    adapter = SimulatedHomeAdapter()
    snapshot = await adapter.discover()
    brightness = next(
        capability
        for entity in snapshot.source_entities
        for capability in entity["capabilities"]
        if capability["name"] == "brightness"
    )
    brightness["maximum"] = 120
    registry = AdapterRegistry()
    registry.register(AdapterRegistration(manifest=_manifest(), factory=SimulatedHomeAdapter))

    report = registry.compatibility("fixture", snapshot)

    assert report.status.value == "degraded"
    assert any(item.code == "capability_guarantee_mismatch" for item in report.diagnostics)


@pytest.mark.asyncio
async def test_provider_malformed_observed_capability_is_degraded() -> None:
    adapter = SimulatedHomeAdapter()
    snapshot = await adapter.discover()
    brightness = next(
        capability
        for entity in snapshot.source_entities
        for capability in entity["capabilities"]
        if capability["name"] == "brightness"
    )
    brightness["guarantees"] = {"expected_latency_ms": -1}
    registry = AdapterRegistry()
    registry.register(AdapterRegistration(manifest=_manifest(), factory=SimulatedHomeAdapter))

    report = registry.compatibility("fixture", snapshot)

    assert report.status.value == "degraded"
    assert any(item.code == "malformed_observed_capability" for item in report.diagnostics)
