from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.adapters.sdk import (
    AdapterManifest,
    AdapterRegistration,
    AdapterRegistry,
    DuplicateAdapterError,
)
from domoai.domain.models import CapabilityKind, DeviceType


def manifest(adapter_id: str = "fixture") -> AdapterManifest:
    return AdapterManifest(
        adapter_id=adapter_id,
        name="Fixture adapter",
        contract_version="v1",
        protocol="fixture",
        package_name="domoai-tests",
        package_version="0.1.0",
        device_types=[DeviceType.LIGHT, DeviceType.SENSOR],
        capabilities=[
            {
                "name": "power",
                "kind": CapabilityKind.BOOLEAN,
                "readable": True,
                "writable": True,
                "commands": ["turn_on", "turn_off", "toggle"],
            }
        ],
    )


def registration(adapter_id: str = "fixture") -> AdapterRegistration:
    factory = SimulatedHomeAdapter if adapter_id == "fixture" else type(
        "ExternalFixtureAdapter",
        (SimulatedHomeAdapter,),
        {"adapter_id": adapter_id},
    )
    return AdapterRegistration(manifest=manifest(adapter_id), factory=factory)


def test_manifest_rejects_credential_like_metadata() -> None:
    payload = manifest().model_dump()
    payload["metadata"] = {"access_token": "secret"}

    with pytest.raises(ValidationError, match="sensitive"):
        AdapterManifest.model_validate(payload)


def test_registry_validates_before_factory_creation() -> None:
    calls = 0

    def factory() -> SimulatedHomeAdapter:
        nonlocal calls
        calls += 1
        return SimulatedHomeAdapter()

    registry = AdapterRegistry()
    registry.register(AdapterRegistration(manifest=manifest(), factory=factory))

    assert calls == 0
    adapter = registry.create("fixture")
    assert adapter.adapter_id == "fixture"
    assert calls == 1


def test_registry_rejects_duplicate_adapter_ids() -> None:
    registry = AdapterRegistry()
    registry.register(registration())

    with pytest.raises(DuplicateAdapterError, match="fixture"):
        registry.register(registration())


def test_registry_does_not_invoke_factory_for_mismatched_adapter_id() -> None:
    registry = AdapterRegistry()
    registry.register(
        AdapterRegistration(
            manifest=manifest("declared"),
            factory=SimulatedHomeAdapter,
        )
    )

    with pytest.raises(ValueError, match="adapter_id"):
        registry.create("declared")


def test_entry_point_failure_is_redacted_and_does_not_abort_other_plugins() -> None:
    good = SimpleNamespace(
        name="fixture",
        group="domoai.adapters",
        load=lambda: lambda: registration("external-fixture"),
    )
    bad = SimpleNamespace(
        name="broken",
        group="domoai.adapters",
        load=lambda: (_ for _ in ()).throw(RuntimeError("access_token=secret")),
    )

    registry = AdapterRegistry.from_entry_points([bad, good])

    assert registry.get("external-fixture") is not None
    assert len(registry.diagnostics) == 1
    assert registry.diagnostics[0].code == "entry_point_load_failed"
    assert "secret" not in registry.diagnostics[0].message
