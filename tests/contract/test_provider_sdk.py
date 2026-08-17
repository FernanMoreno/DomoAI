import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.domain.models import Capability, CapabilityKind, DeviceType, SourceRef
from domoai.domain.provider import (
    DeviceDescriptor,
    Measurement,
    MeasurementQuality,
    ProviderCommand,
    ProviderExecutionResult,
    ProviderManifest,
    ProviderRole,
)
from tests.fixtures.provider_sdk import (
    OBSERVED_AT,
    RECEIVED_AT,
    command_manifest,
    telemetry_manifest,
)

ROOT = Path(__file__).resolve().parents[2]


def test_provider_manifests_support_telemetry_commands_and_combined_roles() -> None:
    telemetry = telemetry_manifest()
    commands = command_manifest()
    combined = telemetry.model_copy(
        update={"roles": [ProviderRole.TELEMETRY, ProviderRole.COMMANDS]}
    )

    assert telemetry.roles == [ProviderRole.TELEMETRY]
    assert commands.roles == [ProviderRole.COMMANDS]
    assert set(combined.roles) == {ProviderRole.TELEMETRY, ProviderRole.COMMANDS}


def test_provider_manifest_is_strict_and_rejects_unsafe_or_duplicate_declarations() -> None:
    with pytest.raises(ValidationError):
        telemetry_manifest().model_validate(
            telemetry_manifest().model_dump(mode="python") | {"unknown": True}
        )

    with pytest.raises(ValidationError, match="sensitive"):
        unsafe = telemetry_manifest().model_dump(mode="python")
        unsafe["metadata"] = {"api_token": "nope"}
        ProviderManifest.model_validate(unsafe)

    duplicate = telemetry_manifest().model_dump(mode="python")
    duplicate["roles"] = ["telemetry", "telemetry"]
    with pytest.raises(ValidationError, match="unique"):
        ProviderManifest.model_validate(duplicate)

    no_roles = telemetry_manifest().model_dump(mode="python")
    no_roles["roles"] = []
    with pytest.raises(ValidationError):
        ProviderManifest.model_validate(no_roles)


def test_descriptor_preserves_provider_local_identity_and_rejects_duplicates() -> None:
    descriptor = DeviceDescriptor(
        provider_id="fixture_telemetry",
        external_id="inverter.main",
        device_type=DeviceType.ENERGY,
        name="Virtual inverter",
        capabilities=telemetry_manifest().capabilities,
        identity_keys=["fixture:inverter.main"],
        connections=["fixture:bus:1"],
    )

    assert descriptor.provider_id == "fixture_telemetry"
    assert descriptor.external_id == "inverter.main"
    with pytest.raises(ValidationError, match="identity_keys must be unique"):
        invalid = descriptor.model_dump(mode="python")
        invalid["identity_keys"] = ["same", "same"]
        DeviceDescriptor.model_validate(invalid)


def test_measurements_normalize_pv_grid_and_battery_with_provenance() -> None:
    source = SourceRef(adapter_id="fixture_telemetry", external_id="inverter.main")
    measurements = [
        Measurement(
            provider_id="fixture_telemetry",
            device_id="inverter.main",
            metric=metric,
            value=value,
            unit=unit,
            observed_at=OBSERVED_AT,
            received_at=RECEIVED_AT,
            quality=MeasurementQuality.GOOD,
            source_ref=source,
        )
        for metric, value, unit in (
            ("energy.pv.power", 4382, "W"),
            ("energy.grid.power", -728, "W"),
            ("battery.soc", 74, "%"),
        )
    ]

    assert [measurement.metric for measurement in measurements] == [
        "energy.pv.power",
        "energy.grid.power",
        "battery.soc",
    ]
    assert all(
        measurement.source_ref.adapter_id == "fixture_telemetry" for measurement in measurements
    )


def test_measurement_rejects_naive_time_invalid_metric_and_mismatched_source() -> None:
    base = {
        "provider_id": "fixture_telemetry",
        "device_id": "inverter.main",
        "metric": "energy.pv.power",
        "value": 1,
        "unit": "W",
        "observed_at": OBSERVED_AT,
        "received_at": RECEIVED_AT,
        "source_ref": {"adapter_id": "fixture_telemetry", "external_id": "inverter.main"},
    }
    with pytest.raises(ValidationError, match="timezone-aware"):
        Measurement.model_validate(base | {"observed_at": datetime(2026, 8, 16, 12)})
    with pytest.raises(ValidationError):
        Measurement.model_validate(base | {"metric": "Energy PV"})
    with pytest.raises(ValidationError, match="adapter_id"):
        Measurement.model_validate(
            base | {"source_ref": {"adapter_id": "other", "external_id": "inverter.main"}}
        )
    with pytest.raises(ValidationError, match="received_at"):
        Measurement.model_validate(base | {"received_at": OBSERVED_AT.replace(hour=11)})


def test_provider_command_rejects_credentials_and_result_round_trips() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        ProviderCommand(
            provider_id="fixture_commands",
            external_device_id="living_room.main_light",
            command="turn_on",
            params={"authorization": "forbidden"},
            idempotency_key="command-1",
        )

    command = ProviderCommand(
        provider_id="fixture_commands",
        external_device_id="living_room.main_light",
        command="turn_on",
        params={"brightness": 60},
        idempotency_key="command-1",
    )
    result = ProviderExecutionResult(
        provider_id="fixture_commands",
        external_device_id=command.external_device_id,
        command=command.command,
        status="confirmed_success",
        completed_at=RECEIVED_AT,
    )
    assert ProviderExecutionResult.model_validate(result.model_dump(mode="python")) == result


def test_provider_contract_schemas_are_published_and_versioned() -> None:
    for name in (
        "provider-manifest",
        "device-descriptor",
        "measurement",
        "provider-command",
        "provider-execution-result",
        "provider-diagnostic",
        "provider-discovery-result",
        "provider-collection-result",
    ):
        schema = json.loads(
            (ROOT / "schemas" / "v1" / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        assert schema["properties"]["schema_version"]["const"] == "v1"


def test_provider_capability_declarations_reject_writable_without_commands() -> None:
    with pytest.raises(ValidationError, match="requires commands"):
        ProviderManifest(
            provider_id="fixture_invalid",
            name="Invalid",
            protocol="fixture",
            package_name="domoai-fixture",
            package_version="1.0.0",
            roles=[ProviderRole.COMMANDS],
            device_types=[DeviceType.LIGHT],
            capabilities=[
                Capability(
                    name="power",
                    kind=CapabilityKind.BOOLEAN,
                    readable=True,
                    writable=True,
                )
            ],
        )
