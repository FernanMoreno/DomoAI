from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from domoai.adapters.home_assistant.config import (
    HomeAssistantBatteryCapacityBinding,
    HomeAssistantDispatchableBatteryBinding,
    HomeAssistantMappingConfigurationError,
)
from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.domain.models import ExecutionStatus, ScalarValue
from domoai.domain.provider import NominalCapacityAttestation, ProviderCommand
from domoai.optimizer.energy import BatteryCapacityEvidence
from domoai.optimizer.providers import battery_soc_observation_from_percentage_measurement
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient


def _capacity_attestation() -> NominalCapacityAttestation:
    return NominalCapacityAttestation(
        evidence_type="vendor_documentation",
        reference="https://www.tesla.com/powerwall",
        subject_model="Powerwall 2",
        attested_by="operator",
        attested_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )


def _command(
    entity_id: str, command: str, key: str, *, value: int | float | None = None
) -> ProviderCommand:
    params: dict[str, ScalarValue] = {"value": value} if value is not None else {}
    return ProviderCommand(
        provider_id="home_assistant",
        external_device_id=entity_id,
        command=command,
        params=params,
        idempotency_key=key,
    )


def _dispatch_binding(**overrides: object) -> HomeAssistantDispatchableBatteryBinding:
    values: dict[str, object] = {
        "device_id": "ha-battery-1",
        "soc_entity_id": "sensor.battery_soc",
        "power_feedback_entity_id": "sensor.battery_power",
        "capacity_entity_id": "sensor.battery_capacity",
        "charge": {
            "entity_id": "cover.battery_command",
            "provider_command": "open",
        },
        "discharge": {
            "entity_id": "cover.battery_command",
            "provider_command": "close",
        },
        "stop": {
            "entity_id": "cover.battery_command",
            "provider_command": "stop",
        },
    }
    values.update(overrides)
    return HomeAssistantDispatchableBatteryBinding.model_validate(values)


def _dispatch_states(**overrides: object) -> list[dict[str, Any]]:
    states: list[dict[str, Any]] = [
        {
            "entity_id": "sensor.battery_soc",
            "state": "74",
            "attributes": {"unit_of_measurement": "%", "device_class": "battery"},
            "device_id": "ha-battery-1",
        },
        {
            "entity_id": "sensor.battery_power",
            "state": "-1.2",
            "attributes": {"unit_of_measurement": "kW", "device_class": "power"},
            "device_id": "ha-battery-1",
        },
        {
            "entity_id": "sensor.battery_capacity",
            "state": "8.0",
            "attributes": {
                "unit_of_measurement": "kWh",
                "device_class": "energy_storage",
            },
            "device_id": "ha-battery-1",
        },
        {
            "entity_id": "cover.battery_command",
            "state": "0",
            "attributes": {"current_position": 0},
            "device_id": "ha-battery-1",
        },
    ]
    for entity_id, changes in overrides.items():
        entity = next(item for item in states if item["entity_id"] == entity_id)
        entity.update(changes if isinstance(changes, dict) else {})
    return states


def _dispatch_provider(
    states: list[dict[str, Any]],
    *,
    binding: HomeAssistantDispatchableBatteryBinding | None = None,
) -> tuple[HomeAssistantProvider, FakeHomeAssistantProviderClient]:
    client = FakeHomeAssistantProviderClient(states)
    provider = HomeAssistantProvider(
        client,
        metric_mappings={
            "sensor.battery_soc": {"battery": "battery.soc"},
            "sensor.battery_power": {"power": "battery.power"},
        },
        battery_capacity_bindings={
            "sensor.battery_capacity": HomeAssistantBatteryCapacityBinding(
                device_id="ha-battery-1",
                semantics="nominal_capacity",
                nominal_capacity_attestation=_capacity_attestation(),
            )
        },
        battery_dispatch_bindings={
            "home-battery": binding or _dispatch_binding(),
        },
    )
    return provider, client


@pytest.mark.asyncio
async def test_provider_validates_dispatch_routes_against_snapshot_without_service_calls() -> None:
    provider, client = _dispatch_provider(_dispatch_states())

    snapshot = await provider.snapshot()
    assert client.fetch_states_calls == 1
    provider.validate_battery_dispatch_routes(snapshot)

    assert client.service_calls == []
    assert client.fetch_states_calls == 1


@pytest.mark.asyncio
async def test_provider_empty_dispatch_bindings_are_a_noop() -> None:
    client = FakeHomeAssistantProviderClient([])
    provider = HomeAssistantProvider(client)

    snapshot = await provider.snapshot()
    provider.validate_battery_dispatch_routes(snapshot)

    assert client.service_calls == []


@pytest.mark.asyncio
async def test_provider_translates_numeric_battery_routes_and_projects_control_capability() -> None:
    states = _dispatch_states()
    states.append(
        {
            "entity_id": "number.battery_command",
            "state": "0",
            "attributes": {
                "unit_of_measurement": "kW",
                "min": -2,
                "max": 2,
                "step": 0.1,
            },
            "device_id": "ha-battery-1",
        }
    )
    binding = _dispatch_binding(
        charge={
            "entity_id": "number.battery_command",
            "provider_command": "charge_battery",
            "service_domain": "number",
            "service": "set_value",
            "value_transform": "as_is",
        },
        discharge={
            "entity_id": "number.battery_command",
            "provider_command": "discharge_battery",
            "service_domain": "number",
            "service": "set_value",
            "value_transform": "negate",
        },
        stop={
            "entity_id": "number.battery_command",
            "provider_command": "stop_battery",
            "service_domain": "number",
            "service": "set_value",
            "value_transform": "zero",
        },
    )
    provider, client = _dispatch_provider(states, binding=binding)

    snapshot = await provider.snapshot()
    provider.validate_battery_dispatch_routes(snapshot)
    command_entity = next(
        entity
        for entity in snapshot.source_entities
        if entity["entity_id"] == "number.battery_command"
    )
    assert any(
        item["name"] == "battery_control" for item in command_entity["capabilities"]
    )

    for command, expected in (
        ("charge_battery", 1.5),
        ("discharge_battery", -1.5),
        ("stop_battery", 0),
    ):
        result = await provider.execute(
            _command("number.battery_command", command, command, value=1.5)
        )
        assert result.status is ExecutionStatus.CONFIRMED_SUCCESS
        assert client.service_calls[-1] == (
            "number",
            "set_value",
            {"entity_id": "number.battery_command", "value": expected},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transform", "minimum", "maximum", "message"),
    [
        ("as_is", -2, 0, "positive"),
        ("negate", 0, 2, "negative"),
    ],
)
async def test_provider_rejects_numeric_route_without_required_sign_range(
    transform: str,
    minimum: int,
    maximum: int,
    message: str,
) -> None:
    states = _dispatch_states()
    states.append(
        {
            "entity_id": "number.battery_command",
            "state": "0",
            "attributes": {
                "unit_of_measurement": "kW",
                "min": minimum,
                "max": maximum,
            },
            "device_id": "ha-battery-1",
        }
    )
    binding = _dispatch_binding(
        charge={
            "entity_id": "number.battery_command",
            "provider_command": "charge_battery",
            "service_domain": "number",
            "service": "set_value",
            "value_transform": transform,
        }
    )
    provider, client = _dispatch_provider(states, binding=binding)

    snapshot = await provider.snapshot()
    with pytest.raises(HomeAssistantMappingConfigurationError, match=message):
        provider.validate_battery_dispatch_routes(snapshot)
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_provider_rejects_value_bearing_legacy_battery_route_before_service_call() -> None:
    provider, client = _dispatch_provider(_dispatch_states())
    await provider.snapshot()

    result = await provider.execute(
        _command("cover.battery_command", "open", "value-bearing-open", value=1.5)
    )

    assert result.status is ExecutionStatus.REJECTED
    assert "numeric" in (result.message or "")
    assert client.service_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("binding_change", "state_change", "expected_message"),
    [
        (
            {"power_feedback_entity_id": "sensor.missing_power"},
            {},
            "source entity",
        ),
        (
            {"device_id": "ha-other-battery"},
            {},
            "device",
        ),
        (
            {},
            {
                "sensor.battery_power": {
                    "attributes": {"unit_of_measurement": "W", "device_class": "power"}
                }
            },
            "power feedback",
        ),
        (
            {},
            {"sensor.battery_soc": {"available": False}},
            "unavailable",
        ),
        (
            {},
            {
                "sensor.battery_capacity": {
                    "attributes": {
                        "unit_of_measurement": "kWh",
                        "device_class": "power",
                    }
                }
            },
            "capacity",
        ),
        (
            {
                "charge": {
                    "entity_id": "cover.battery_command",
                    "provider_command": "set_position",
                }
            },
            {},
            "translated",
        ),
    ],
)
async def test_provider_rejects_invalid_dispatch_routes_without_fallback_or_service(
    binding_change: dict[str, object],
    state_change: dict[str, object],
    expected_message: str,
) -> None:
    provider, client = _dispatch_provider(
        _dispatch_states(**state_change),
        binding=_dispatch_binding(**binding_change),
    )

    snapshot = await provider.snapshot()

    with pytest.raises(HomeAssistantMappingConfigurationError, match=expected_message):
        provider.validate_battery_dispatch_routes(snapshot)
    assert client.service_calls == []
    assert client.fetch_states_calls == 1


@pytest.mark.asyncio
async def test_provider_groups_entities_and_preserves_registry_metadata() -> None:
    states: list[dict[str, Any]] = [
        {
            "entity_id": "light.multi_power",
            "state": "off",
            "attributes": {"friendly_name": "Multi light"},
            "device_id": "ha-multi-1",
            "area_id": "living_room",
            "manufacturer": "Fixture Lamps",
            "model": "L100",
            "identifiers": ["fixture:multi-1"],
            "connections": [["mac", "AA:BB:CC:DD:EE:FF"]],
            "via_device": "ha-bridge-1",
        },
        {
            "entity_id": "light.multi_brightness",
            "state": "on",
            "attributes": {"friendly_name": "Multi light brightness", "brightness": 128},
            "device_id": "ha-multi-1",
            "area_id": "living_room",
        },
    ]
    provider = HomeAssistantProvider(FakeHomeAssistantProviderClient(states))

    descriptors = await provider.discover()

    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert descriptor.external_id == "ha-multi-1"
    assert descriptor.area_id == "living_room"
    assert descriptor.manufacturer == "Fixture Lamps"
    assert descriptor.model == "L100"
    assert descriptor.parent_external_id == "ha-bridge-1"
    assert descriptor.identity_keys == ["fixture:multi-1"]
    assert descriptor.connections == [
        "light.multi_power",
        "mac:AA:BB:CC:DD:EE:FF",
        "light.multi_brightness",
    ]
    assert {capability.name for capability in descriptor.capabilities} == {"power", "brightness"}


@pytest.mark.asyncio
async def test_provider_uses_entity_registry_when_states_lack_device_id() -> None:
    client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "light.registry_light",
                "state": "on",
                "attributes": {"friendly_name": "Registry light"},
            },
            {
                "entity_id": "sensor.registry_power",
                "state": "120",
                "attributes": {
                    "unit_of_measurement": "W",
                    "device_class": "power",
                },
            },
        ],
        registry=[
            {"entity_id": "light.registry_light", "device_id": "ha-registry-1"},
            {"entity_id": "sensor.registry_power", "device_id": "ha-registry-1"},
        ],
    )
    provider = HomeAssistantProvider(
        client,
        metric_mappings={"sensor.registry_power": {"power": "energy.home.power"}},
    )

    descriptors = await provider.discover()
    measurements = await provider.get_measurements()

    assert [descriptor.external_id for descriptor in descriptors] == ["ha-registry-1"]
    assert [
        (item.device_id, item.metric, item.value)
        for item in measurements
        if item.metric == "energy.home.power"
    ] == [("ha-registry-1", "energy.home.power", 120)]


@pytest.mark.asyncio
async def test_provider_maps_energy_metrics_without_guessing_roles() -> None:
    states = [
        {
            "entity_id": "sensor.pv_power",
            "state": "4382",
            "last_updated": "2026-08-17T10:00:00+00:00",
            "attributes": {"unit_of_measurement": "W", "device_class": "power"},
            "device_id": "ha-energy-1",
        },
        {
            "entity_id": "sensor.unknown_power",
            "state": "12",
            "attributes": {"unit_of_measurement": "W", "device_class": "power"},
            "device_id": "ha-energy-1",
        },
        {
            "entity_id": "sensor.battery_soc",
            "state": "74",
            "attributes": {"unit_of_measurement": "%", "device_class": "battery"},
            "device_id": "ha-battery-1",
        },
    ]
    provider = HomeAssistantProvider(
        FakeHomeAssistantProviderClient(states),
        metric_mappings={
            "sensor.pv_power": {"power": "energy.pv.power"},
            "sensor.battery_soc": {"battery": "battery.soc"},
        },
    )

    measurements = await provider.get_measurements()

    assert {(item.metric, item.value) for item in measurements} == {
        ("energy.pv.power", 4382),
        ("battery.soc", 74),
    }
    raw_soc = next(item for item in measurements if item.metric == "battery.soc")
    assert raw_soc.unit == "%"
    converted = battery_soc_observation_from_percentage_measurement(
        raw_soc,
        BatteryCapacityEvidence(
            provider_id="home_assistant",
            device_id="ha-battery-1",
            capacity_kwh=8.0,
        ),
    )
    assert converted.value_kwh == 5.92
    assert converted.conversion_evidence is not None
    assert all(
        item.observed_at == datetime(2026, 8, 17, 10, tzinfo=UTC)
        for item in measurements
        if item.metric == "energy.pv.power"
    )
    assert all(item.source_ref.external_id != "sensor.unknown_power" for item in measurements)


@pytest.mark.asyncio
async def test_provider_translates_only_declared_commands_and_sanitizes_failures() -> None:
    client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "light.living_room",
                "state": "off",
                "attributes": {},
                "device_id": "ha-light-1",
            }
        ],
        fail_services=True,
    )
    provider = HomeAssistantProvider(client)
    await provider.discover()

    failed = await provider.execute(_command("light.living_room", "turn_on", "failure"))
    unknown = await provider.execute(_command("light.unknown", "turn_on", "unknown"))
    unsupported = await provider.execute(_command("light.living_room", "set_color", "color"))

    assert failed.status is ExecutionStatus.FAILED
    assert failed.message == "Home Assistant service call failed"
    assert "token" not in (failed.message or "")
    assert unknown.status is ExecutionStatus.REJECTED
    assert unsupported.status is ExecutionStatus.REJECTED

    ok_client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "light.living_room",
                "state": "off",
                "attributes": {},
                "device_id": "ha-light-1",
            }
        ]
    )
    ok_provider = HomeAssistantProvider(ok_client)
    await ok_provider.discover()
    first = await ok_provider.execute(_command("light.living_room", "turn_on", "same"))
    duplicate = await ok_provider.execute(_command("light.living_room", "turn_on", "same"))

    assert first.status is ExecutionStatus.CONFIRMED_SUCCESS
    assert duplicate.status is ExecutionStatus.REJECTED
    assert ok_client.service_calls == [("light", "turn_on", {"entity_id": "light.living_room"})]


@pytest.mark.asyncio
async def test_provider_subscription_reuses_polling_mapping() -> None:
    client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "sensor.pv_power",
                "state": "100",
                "attributes": {"unit_of_measurement": "W", "device_class": "power"},
                "device_id": "ha-energy-1",
            }
        ],
        events=[
            {
                "type": "event",
                "event": {
                    "data": {
                        "entity_id": "sensor.pv_power",
                        "new_state": {
                            "state": "250",
                            "last_updated": "2026-08-17T11:00:00+00:00",
                            "attributes": {
                                "unit_of_measurement": "W",
                                "device_class": "power",
                            },
                        },
                    }
                },
            }
        ],
    )
    provider = HomeAssistantProvider(
        client, metric_mappings={"sensor.pv_power": {"power": "energy.pv.power"}}
    )
    await provider.discover()

    measurement = await anext(provider.subscribe())

    assert measurement.metric == "energy.pv.power"
    assert measurement.value == 250
    assert measurement.observed_at == datetime(2026, 8, 17, 11, tzinfo=UTC)


@pytest.mark.asyncio
async def test_provider_emits_explicit_nominal_capacity_measurement() -> None:
    client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "sensor.battery_capacity",
                "state": "8.0",
                "attributes": {"unit_of_measurement": "kWh", "device_class": "energy_storage"},
                "device_id": "ha-battery-1",
            },
            {
                "entity_id": "sensor.battery_soc",
                "state": "50",
                "attributes": {"unit_of_measurement": "%", "device_class": "battery"},
                "device_id": "ha-battery-1",
            },
        ]
    )
    provider = HomeAssistantProvider(
        client,
        battery_capacity_bindings={
            "sensor.battery_capacity": HomeAssistantBatteryCapacityBinding(
                device_id="ha-battery-1",
                semantics="nominal_capacity",
                nominal_capacity_attestation=_capacity_attestation(),
            )
        },
        metric_mappings={"sensor.battery_soc": {"battery": "battery.soc"}},
    )

    measurements = await provider.get_measurements()

    capacity = next(item for item in measurements if item.metric == "battery.capacity")
    assert capacity.value == 8.0
    assert capacity.unit == "kWh"
    assert capacity.device_id == "ha-battery-1"
    assert capacity.source_ref.external_id == "sensor.battery_capacity"
    assert capacity.nominal_capacity_attestation == _capacity_attestation()


@pytest.mark.asyncio
@pytest.mark.parametrize("device_class", [None, "power"])
async def test_provider_rejects_capacity_binding_without_energy_storage_class(
    device_class: str | None,
) -> None:
    attributes: dict[str, str] = {"unit_of_measurement": "kWh"}
    if device_class is not None:
        attributes["device_class"] = device_class
    client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "sensor.battery_capacity",
                "state": "8.0",
                "attributes": attributes,
                "device_id": "ha-battery-1",
            }
        ]
    )
    provider = HomeAssistantProvider(
        client,
        battery_capacity_bindings={
            "sensor.battery_capacity": HomeAssistantBatteryCapacityBinding(
                device_id="ha-battery-1",
                semantics="nominal_capacity",
                nominal_capacity_attestation=_capacity_attestation(),
            )
        },
    )

    with pytest.raises(HomeAssistantMappingConfigurationError, match="energy_storage"):
        await provider.get_measurements()


@pytest.mark.asyncio
async def test_provider_rejects_capacity_binding_for_another_device() -> None:
    client = FakeHomeAssistantProviderClient(
        [
            {
                "entity_id": "sensor.battery_capacity",
                "state": "8.0",
                "attributes": {"unit_of_measurement": "kWh", "device_class": "energy_storage"},
                "device_id": "ha-battery-2",
            }
        ]
    )
    provider = HomeAssistantProvider(
        client,
        battery_capacity_bindings={
            "sensor.battery_capacity": HomeAssistantBatteryCapacityBinding(
                device_id="ha-battery-1",
                semantics="nominal_capacity",
                nominal_capacity_attestation=_capacity_attestation(),
            )
        },
    )

    with pytest.raises(HomeAssistantMappingConfigurationError, match="device"):
        await provider.get_measurements()
