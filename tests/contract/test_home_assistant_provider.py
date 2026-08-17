from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.domain.models import ExecutionStatus, ScalarValue
from domoai.domain.provider import ProviderCommand
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient


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
