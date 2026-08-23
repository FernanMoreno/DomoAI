from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import SourceRef
from domoai.domain.provider import MeasurementQuality
from domoai.optimizer.energy import (
    BatteryActuator,
    BatteryCapacityEvidence,
    BatteryProfile,
    BatterySocObservation,
    DispatchableBatteryBinding,
)
from domoai.optimizer.providers import ComposedEnergyContextProvider, StateStoreBatteryProvider


def _binding() -> DispatchableBatteryBinding:
    observed_at = datetime(2026, 8, 23, 12, tzinfo=UTC)
    return DispatchableBatteryBinding(
        provider_id="home_assistant",
        device_id="battery.home",
        profile=BatteryProfile(
            capacity_kwh=8.0,
            initial_soc_kwh=4.0,
            min_soc_kwh=0.0,
            max_soc_kwh=8.0,
            max_charge_kw=2.0,
            max_discharge_kw=2.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
            actuator=BatteryActuator(
                device_id="battery.home",
                capability="battery_control",
                charge_command="charge_battery",
                discharge_command="discharge_battery",
                stop_command="stop_battery",
                power_feedback_capability="battery.power",
                power_feedback_tolerance_kw=0.1,
                soc_reconciliation_capability="battery.soc",
            ),
            initial_soc_observation=BatterySocObservation(
                provider_id="home_assistant",
                device_id="battery.home",
                metric="battery.soc",
                value_kwh=4.0,
                observed_at=observed_at,
                received_at=observed_at,
                quality=MeasurementQuality.GOOD,
                source_ref=SourceRef(adapter_id="home_assistant", external_id="sensor.battery_soc"),
            ),
        ),
        capacity_evidence=BatteryCapacityEvidence(
            provider_id="home_assistant",
            device_id="battery.home",
            capacity_kwh=8.0,
            capacity_source="provider_config",
            quality=MeasurementQuality.GOOD,
            observed_at=observed_at,
            received_at=observed_at,
            source_ref=SourceRef(
                adapter_id="home_assistant", external_id="sensor.battery_capacity"
            ),
        ),
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_standard_runtime_loads_server_owned_battery_profile(tmp_path) -> None:
    profile_path = tmp_path / "dispatchable-battery-profile.json"
    profile_path.write_text(json.dumps(_binding().model_dump(mode="json")), encoding="utf-8")
    settings = Settings(
        database_path=tmp_path / "runtime.sqlite3",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6.0,
        solar_tilt=30.0,
        solar_azimuth=0.0,
        solar_performance_ratio=0.82,
        battery_dispatch_profile_path=profile_path,
    )

    runtime = await build_runtime(settings, adapter=SimulatedHomeAdapter())
    try:
        assert isinstance(runtime.battery_provider, StateStoreBatteryProvider)
        assert isinstance(runtime.energy_context_provider, ComposedEnergyContextProvider)
        assert runtime.energy_context_provider.battery is runtime.battery_provider
        assert runtime.battery_provider.device_id == "battery.home"
    finally:
        await runtime.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_profile_and_programmatic_binding_cannot_be_combined(tmp_path) -> None:
    profile_path = tmp_path / "dispatchable-battery-profile.json"
    profile_path.write_text(json.dumps(_binding().model_dump(mode="json")), encoding="utf-8")
    settings = Settings(
        database_path=tmp_path / "conflict.sqlite3",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6.0,
        solar_tilt=30.0,
        solar_azimuth=0.0,
        solar_performance_ratio=0.82,
        battery_dispatch_profile_path=profile_path,
    )

    with pytest.raises(ValueError, match="either by profile path or argument"):
        await build_runtime(
            settings,
            adapter=SimulatedHomeAdapter(),
            dispatchable_battery_binding=_binding(),
        )
