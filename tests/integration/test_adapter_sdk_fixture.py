from __future__ import annotations

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.sdk import (
    AdapterManifest,
    AdapterRegistration,
    AdapterRegistry,
    ConformanceHarness,
)
from domoai.domain.models import CapabilityKind, DeviceType
from domoai.runtime.composite_adapter import CompositeAdapter


def fixture_manifest(adapter_id: str = "fixture") -> AdapterManifest:
    return AdapterManifest(
        adapter_id=adapter_id,
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
                "name": "position",
                "kind": CapabilityKind.INTEGER,
                "unit": "%",
                "readable": True,
                "writable": True,
                "minimum": 0,
                "maximum": 100,
                "commands": ["set_position", "open", "close", "stop"],
            },
            {
                "name": "temperature",
                "kind": CapabilityKind.NUMBER,
                "unit": "°C",
                "readable": True,
                "writable": False,
            },
            {
                "name": "target_temperature",
                "kind": CapabilityKind.NUMBER,
                "unit": "°C",
                "readable": True,
                "writable": True,
                "minimum": 16,
                "maximum": 27,
                "commands": ["set_temperature"],
            },
            {
                "name": "power_consumption",
                "kind": CapabilityKind.NUMBER,
                "unit": "W",
                "readable": True,
                "writable": False,
                "optional": True,
            },
        ],
    )


@pytest.mark.asyncio
async def test_simulated_home_passes_sdk_conformance() -> None:
    registration = AdapterRegistration(
        manifest=fixture_manifest(), factory=SimulatedHomeAdapter
    )

    result = await ConformanceHarness(registration).run()

    assert result.status == "passed"
    assert all(check.status == "passed" for check in result.checks)
    assert [check.check_id for check in result.checks] == sorted(
        check.check_id for check in result.checks
    )


@pytest.mark.asyncio
async def test_registered_adapter_can_be_composed_without_runtime_edits() -> None:
    class ExternalFixtureAdapter(SimulatedHomeAdapter):
        adapter_id = "external-fixture"

    registry = AdapterRegistry()
    registry.register(
        AdapterRegistration(
            manifest=fixture_manifest("external-fixture"), factory=ExternalFixtureAdapter
        )
    )
    external = registry.create("external-fixture")
    composite = CompositeAdapter([SimulatedHomeAdapter(), external])

    await composite.connect()
    snapshot = await composite.discover()
    await composite.disconnect()

    assert {item["source_adapter_id"] for item in snapshot.source_entities} == {
        "fixture",
        "external-fixture",
    }
