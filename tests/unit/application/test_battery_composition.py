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
from domoai.domain.models import SourceRef
from domoai.domain.provider import MeasurementQuality, NominalCapacityAttestation
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryProfile,
    BatterySocObservation,
)
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient


def _attestation() -> NominalCapacityAttestation:
    return NominalCapacityAttestation(
        evidence_type="vendor_documentation",
        reference="https://vendor.example/battery",
        subject_model="Battery",
        attested_by="operator",
        attested_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )


def _states() -> list[dict[str, Any]]:
    return [
        {
            "entity_id": "sensor.battery_soc",
            "state": "50",
            "attributes": {"unit_of_measurement": "%", "device_class": "battery"},
            "device_id": "ha-battery-1",
        },
        {
            "entity_id": "sensor.battery_power",
            "state": "0",
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


def _provider() -> tuple[HomeAssistantProvider, FakeHomeAssistantProviderClient]:
    client = FakeHomeAssistantProviderClient(_states())
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
                nominal_capacity_attestation=_attestation(),
            )
        },
        battery_dispatch_bindings={
            "home-battery": HomeAssistantDispatchableBatteryBinding.model_validate(
                {
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
            )
        },
    )
    return provider, client


def _profile(**actuator_changes: object) -> BatteryProfile:
    actuator = BatteryActuator(
        device_id="battery.home",
        capability="battery_control",
        charge_command="open",
        discharge_command="close",
        stop_command="stop",
        power_feedback_capability="battery.power",
        power_feedback_tolerance_kw=0.1,
        soc_reconciliation_capability="battery.soc",
    ).model_copy(update=actuator_changes)
    observation = BatterySocObservation(
        provider_id="home_assistant",
        device_id="battery.home",
        metric="battery.soc",
        value_kwh=4.0,
        observed_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
        received_at=datetime(2026, 8, 22, 12, tzinfo=UTC),
        quality=MeasurementQuality.GOOD,
        source_ref=SourceRef(
            adapter_id="home_assistant", external_id="sensor.battery_soc"
        ),
    )
    return BatteryProfile(
        capacity_kwh=8.0,
        initial_soc_kwh=4.0,
        min_soc_kwh=0.0,
        max_soc_kwh=8.0,
        max_charge_kw=2.0,
        max_discharge_kw=2.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        actuator=actuator,
        initial_soc_observation=observation,
    )


def _capacity(**changes: object) -> BatteryCapacityEvidence:
    values: dict[str, object] = {
        "provider_id": "home_assistant",
        "device_id": "battery.home",
        "capacity_kwh": 8.0,
    }
    values.update(changes)
    return BatteryCapacityEvidence.model_validate(values)


@pytest.mark.asyncio
async def test_composes_validated_home_assistant_routes_into_canonical_binding() -> None:
    from domoai.application.battery_composition import (
        compose_home_assistant_dispatchable_battery_binding,
    )

    provider, client = _provider()
    snapshot = await provider.snapshot()

    binding = compose_home_assistant_dispatchable_battery_binding(
        provider,
        snapshot,
        binding_id="home-battery",
        canonical_device_id="battery.home",
        profile=_profile(),
        capacity_evidence=_capacity(),
    )

    assert binding.provider_id == "home_assistant"
    assert binding.device_id == "battery.home"
    assert binding.profile.actuator is not None
    assert binding.profile.actuator.capability == "battery_control"
    assert client.fetch_states_calls == 1
    assert client.service_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": "other-battery"},
        {"charge_command": "toggle"},
        {"capability": "battery_power"},
        {"power_feedback_capability": "battery.other_power"},
        {"soc_reconciliation_capability": "battery.other_soc"},
    ],
)
async def test_rejects_incoherent_actuator_before_any_physical_call(
    changes: dict[str, object],
) -> None:
    from domoai.application.battery_composition import (
        compose_home_assistant_dispatchable_battery_binding,
    )

    provider, client = _provider()
    snapshot = await provider.snapshot()

    with pytest.raises(HomeAssistantMappingConfigurationError):
        compose_home_assistant_dispatchable_battery_binding(
            provider,
            snapshot,
            binding_id="home-battery",
            canonical_device_id="battery.home",
            profile=_profile(**changes),
            capacity_evidence=_capacity(),
        )

    assert client.fetch_states_calls == 1
    assert client.service_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"device_id": "other-battery"},
        {"provider_id": "other_provider"},
        {"capacity_kwh": 7.0},
    ],
)
async def test_rejects_incoherent_capacity_evidence(
    changes: dict[str, object],
) -> None:
    from domoai.application.battery_composition import (
        compose_home_assistant_dispatchable_battery_binding,
    )

    provider, client = _provider()
    snapshot = await provider.snapshot()

    with pytest.raises(HomeAssistantMappingConfigurationError):
        compose_home_assistant_dispatchable_battery_binding(
            provider,
            snapshot,
            binding_id="home-battery",
            canonical_device_id="battery.home",
            profile=_profile(),
            capacity_evidence=_capacity(**changes),
        )

    assert client.fetch_states_calls == 1
    assert client.service_calls == []


@pytest.mark.asyncio
async def test_rejects_capacity_evidence_that_disagrees_with_snapshot() -> None:
    from domoai.application.battery_composition import (
        compose_home_assistant_dispatchable_battery_binding,
    )

    provider, client = _provider()
    snapshot = await provider.snapshot()
    profile = _profile().model_copy(update={"capacity_kwh": 9.0})

    with pytest.raises(HomeAssistantMappingConfigurationError, match="snapshot"):
        compose_home_assistant_dispatchable_battery_binding(
            provider,
            snapshot,
            binding_id="home-battery",
            canonical_device_id="battery.home",
            profile=profile,
            capacity_evidence=_capacity(capacity_kwh=9.0),
        )

    assert client.fetch_states_calls == 1
    assert client.service_calls == []
