from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCapacityBinding,
    HomeAssistantDispatchableBatteryBinding,
    HomeAssistantMappingConfigurationError,
    load_battery_capacity_bindings,
    load_battery_dispatch_bindings,
    load_metric_mappings,
)
from domoai.domain.provider import NominalCapacityAttestation

ATTESTATION = NominalCapacityAttestation(
    evidence_type="vendor_documentation",
    reference="https://www.tesla.com/powerwall",
    subject_model="Powerwall 2",
    attested_by="operator",
    attested_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
)


def _dispatch_payload(*, device_id: str = "ha-battery-1") -> dict[str, object]:
    return {
        "schema_version": "v1",
        "battery_capacity_bindings": {
            "sensor.powerwall_capacity": {
                "device_id": device_id,
                "semantics": "nominal_capacity",
                "nominal_capacity_attestation": ATTESTATION.model_dump(mode="json"),
            }
        },
        "battery_dispatch_bindings": {
            "home-battery": {
                "schema_version": "v1",
                "device_id": device_id,
                "soc_entity_id": "sensor.powerwall_soc",
                "power_feedback_entity_id": "sensor.powerwall_power",
                "capacity_entity_id": "sensor.powerwall_capacity",
                "capacity_metric": "battery.capacity",
                "charge": {
                    "entity_id": "number.powerwall_command",
                    "provider_command": "charge",
                },
                "discharge": {
                    "entity_id": "number.powerwall_command",
                    "provider_command": "discharge",
                },
                "stop": {
                    "entity_id": "number.powerwall_command",
                    "provider_command": "stop",
                },
            }
        },
    }


def test_load_metric_mappings_accepts_strict_v1_document(tmp_path: Path) -> None:
    path = tmp_path / "home-assistant-mappings.json"
    path.write_text(
        '{"schema_version":"v1","metric_mappings":{"sensor.pv_power":{"power":"energy.pv.power"}}}',
        encoding="utf-8",
    )

    assert load_metric_mappings(path) == {"sensor.pv_power": {"power": "energy.pv.power"}}


def test_load_battery_capacity_bindings_accepts_explicit_device_binding(tmp_path: Path) -> None:
    path = tmp_path / "home-assistant-mappings.json"
    path.write_text(
        '{"schema_version":"v1","battery_capacity_bindings":{'
        '"sensor.battery_capacity":{"device_id":"ha-battery-1",'
        '"semantics":"nominal_capacity",'
        '"nominal_capacity_attestation":{"evidence_type":"vendor_documentation",'
        '"reference":"https://www.tesla.com/powerwall",'
        '"subject_model":"Powerwall 2","attested_by":"operator",'
        '"attested_at":"2026-08-22T12:00:00Z"}}}}',
        encoding="utf-8",
    )

    bindings = load_battery_capacity_bindings(path)

    assert bindings == {
        "sensor.battery_capacity": HomeAssistantBatteryCapacityBinding(
            device_id="ha-battery-1",
            semantics="nominal_capacity",
            nominal_capacity_attestation=ATTESTATION,
        )
    }


def test_load_battery_dispatch_bindings_preserves_explicit_routes(tmp_path: Path) -> None:
    path = tmp_path / "home-assistant-mappings.json"
    path.write_text(json_dumps(_dispatch_payload()), encoding="utf-8")

    bindings = load_battery_dispatch_bindings(path)

    binding = bindings["home-battery"]
    assert isinstance(binding, HomeAssistantDispatchableBatteryBinding)
    assert binding.device_id == "ha-battery-1"
    assert binding.soc_entity_id == "sensor.powerwall_soc"
    assert binding.power_feedback_entity_id == "sensor.powerwall_power"
    assert binding.charge.entity_id == "number.powerwall_command"
    assert binding.charge.provider_command == "charge"
    assert binding.stop.provider_command == "stop"


def test_load_battery_dispatch_bindings_accepts_numeric_setpoint_routes(tmp_path: Path) -> None:
    payload = _dispatch_payload()
    binding = payload["battery_dispatch_bindings"]
    assert isinstance(binding, dict)
    routes = binding["home-battery"]
    assert isinstance(routes, dict)
    routes["charge"] = {
        "entity_id": "number.powerwall_command",
        "provider_command": "charge_battery",
        "service_domain": "number",
        "service": "set_value",
        "value_transform": "as_is",
    }
    routes["discharge"] = {
        "entity_id": "number.powerwall_command",
        "provider_command": "discharge_battery",
        "service_domain": "number",
        "service": "set_value",
        "value_transform": "negate",
    }
    routes["stop"] = {
        "entity_id": "number.powerwall_command",
        "provider_command": "stop_battery",
        "service_domain": "number",
        "service": "set_value",
        "value_transform": "zero",
    }
    path = tmp_path / "numeric.json"
    path.write_text(json_dumps(payload), encoding="utf-8")

    loaded = load_battery_dispatch_bindings(path)["home-battery"]

    assert loaded.control_capability == "battery_control"
    assert loaded.charge.service_domain == "number"
    assert loaded.charge.service == "set_value"
    assert loaded.discharge.value_transform == "negate"
    assert loaded.stop.value_transform == "zero"


@pytest.mark.parametrize(
    "route_change",
    [
        {"service_domain": "number"},
        {"service": "set_value"},
        {"service_domain": "number", "service": "set_value"},
    ],
)
def test_numeric_route_requires_an_explicit_value_transform(
    route_change: dict[str, object],
) -> None:
    payload = _dispatch_payload()
    dispatch = payload["battery_dispatch_bindings"]
    assert isinstance(dispatch, dict)
    charge = dispatch["home-battery"]["charge"]
    assert isinstance(charge, dict)
    charge.update(route_change)

    with pytest.raises(ValueError):
        HomeAssistantDispatchableBatteryBinding.model_validate(dispatch["home-battery"])


def test_dispatch_binding_rejects_capacity_reference_from_another_device(
    tmp_path: Path,
) -> None:
    payload = _dispatch_payload()
    capacity_binding = payload["battery_capacity_bindings"]
    assert isinstance(capacity_binding, dict)
    entry = capacity_binding["sensor.powerwall_capacity"]
    assert isinstance(entry, dict)
    entry["device_id"] = "ha-other-battery"
    path = tmp_path / "invalid.json"
    path.write_text(json_dumps(payload), encoding="utf-8")

    with pytest.raises(HomeAssistantMappingConfigurationError):
        load_battery_dispatch_bindings(path)


@pytest.mark.parametrize(
    "change",
    [
        {"soc_entity_id": "battery-name"},
        {"power_feedback_entity_id": "living room battery power"},
        {"charge": {"entity_id": "number.command", "provider_command": ""}},
        {
            "stop": {
                "entity_id": "number.powerwall_command",
                "provider_command": "charge",
            }
        },
    ],
)
def test_dispatch_binding_rejects_invalid_routes(tmp_path: Path, change: dict[str, object]) -> None:
    payload = _dispatch_payload()
    binding = payload["battery_dispatch_bindings"]
    assert isinstance(binding, dict)
    home_binding = binding["home-battery"]
    assert isinstance(home_binding, dict)
    home_binding.update(change)
    path = tmp_path / "invalid.json"
    path.write_text(json_dumps(payload), encoding="utf-8")

    with pytest.raises(HomeAssistantMappingConfigurationError):
        load_battery_dispatch_bindings(path)


def json_dumps(payload: object) -> str:
    import json

    return json.dumps(payload, separators=(",", ":"))


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"schema_version":"v2","metric_mappings":{}}',
        '{"schema_version":"v1","metric_mappings":{},"token":"secret"}',
        '{"schema_version":"v1","metric_mappings":{"sensor.x":{"power":""}}}',
        '{"schema_version":"v1","battery_capacity_bindings":{"sensor.x":{}}}',
        '{"schema_version":"v1","battery_capacity_bindings":{"sensor.x":{'
        '"device_id":"ha-1","semantics":"other"}}}',
        '{"schema_version":"v1","battery_capacity_bindings":{"sensor.x":{'
        '"device_id":"ha-1","semantics":"nominal_capacity",'
        '"nominal_capacity_attestation":{"evidence_type":"vendor_documentation",'
        '"reference":"https://example.test/capacity","subject_model":"Battery",'
        '"attested_by":"operator","attested_at":"2026-08-22T12:00:00"}}}}',
        '{"schema_version":"v1","metric_mappings":{"sensor.x":{"power":"x"}},'
        '"battery_capacity_bindings":{"sensor.x":{"device_id":"ha-1",'
        '"semantics":"nominal_capacity"}}}',
    ],
)
def test_load_metric_mappings_rejects_invalid_or_sensitive_documents(
    tmp_path: Path, payload: str
) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(HomeAssistantMappingConfigurationError):
        load_metric_mappings(path)
