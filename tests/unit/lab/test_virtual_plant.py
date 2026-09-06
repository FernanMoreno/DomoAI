from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulator
from domoai.lab.ev_charging_simulator import EVChargingSimulationProfile, EVChargingSimulator
from domoai.lab.thermal_simulator import ThermalSimulationProfile, ThermalSimulator
from domoai.lab.virtual_plant import VirtualHomePlant, VirtualPlantClock
from domoai.lab.water_consumption_simulator import (
    WaterConsumptionSimulationProfile,
    WaterConsumptionSimulator,
)


def test_virtual_clock_advances_without_wall_clock_sleep() -> None:
    initial = datetime(2026, 9, 4, 12, tzinfo=UTC)
    clock = VirtualPlantClock(initial)

    clock.advance(timedelta(minutes=5))

    assert clock.now() == initial + timedelta(minutes=5)


def test_virtual_plant_command_changes_state_once_and_records_trace() -> None:
    plant = VirtualHomePlant.default(seed=187)
    before = plant.read("fixture", "fixture.light", "power")

    first = plant.command("fixture", "fixture.light", "turn_on", idempotency_key="twin-command-1")
    duplicate = plant.command(
        "fixture", "fixture.light", "turn_on", idempotency_key="twin-command-1"
    )

    assert before.value is False
    assert first.value is True
    assert duplicate.value is True
    assert plant.write_count == 1
    assert [entry["operation"] for entry in plant.trace][-3:] == [
        "read",
        "command",
        "command_duplicate",
    ]


def test_virtual_plant_mounts_all_physical_models_and_preserves_bounds() -> None:
    clock = VirtualPlantClock(datetime(2026, 9, 4, tzinfo=UTC))
    plant = VirtualHomePlant.default(seed=187, clock=clock)
    battery = BatterySimulator(
        BatterySimulationProfile(
            provider_id="lab-battery",
            device_id="lab-battery-1",
            capacity_kwh=10,
            initial_soc_kwh=5,
            min_soc_kwh=1,
            max_soc_kwh=9,
            max_charge_kw=4,
            max_discharge_kw=4,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
        ),
        clock=clock,
    )
    ev = EVChargingSimulator(
        EVChargingSimulationProfile(
            provider_id="lab-ev",
            device_id="lab-ev-1",
            capacity_kwh=60,
            initial_soc_kwh=20,
            max_charge_kw=7,
            charge_efficiency=0.9,
        ),
        clock=clock,
    )
    thermal = ThermalSimulator(
        ThermalSimulationProfile(
            provider_id="lab-thermal",
            device_id="lab-thermal-1",
            capacitance_kwh_per_c=0.5,
            ua_kw_per_c=0.05,
            initial_temperature_c=20,
            initial_exterior_temperature_c=10,
            max_heat_kw=2,
            max_cool_kw=2,
            heating_cop=3,
            cooling_cop=2.5,
        ),
        clock=clock,
    )
    water = WaterConsumptionSimulator(
        WaterConsumptionSimulationProfile(
            provider_id="lab-water",
            device_id="lab-water-1",
            initial_flow_rate_lpm=3,
        ),
        clock=clock,
    )
    for simulator in (battery, ev, thermal, water):
        plant.mount(simulator)

    battery.command("charge_battery", value=4, idempotency_key="battery-1")
    ev.command("charge_ev", value=7, idempotency_key="ev-1")
    plant.tick(timedelta(hours=1))

    snapshots = plant.model_snapshots()
    assert {
        "lab-battery-1",
        "lab-ev-1",
        "lab-thermal-1",
        "lab-water-1",
    }.issubset(snapshots)
    assert 1 <= snapshots["lab-battery-1"]["soc_kwh"] <= 9
    assert 0 <= snapshots["lab-ev-1"]["soc_kwh"] <= 60
    assert snapshots["lab-water-1"]["total_volume_l"] >= 0
    assert plant.invariant_violations() == []
    assert plant.revision > 0


def test_virtual_plant_unavailable_fault_rejects_command_without_write() -> None:
    plant = VirtualHomePlant.default(seed=187)
    plant.set_fault("fixture", "fixture.light", "unavailable")

    with pytest.raises(ConnectionError):
        plant.command("fixture", "fixture.light", "turn_on", idempotency_key="unavailable-1")

    assert plant.write_count == 0
    assert plant.read("fixture", "fixture.light", "power").available is False


def test_default_plant_mounts_domain_simulators_and_projects_tick_state() -> None:
    plant = VirtualHomePlant.default(seed=187)
    before = plant.model_snapshots()

    plant.command(
        "modbus", "modbus.battery", "charge_battery", value=4, idempotency_key="battery-default"
    )
    plant.command("modbus", "modbus.ev", "charge_ev", value=7, idempotency_key="ev-default")
    plant.tick(timedelta(hours=1))

    after = plant.model_snapshots()
    assert set(after) == {"modbus.battery", "modbus.ev", "modbus.thermal", "modbus.water"}
    assert after["modbus.battery"]["soc_kwh"] > before["modbus.battery"]["soc_kwh"]
    assert after["modbus.ev"]["soc_kwh"] > before["modbus.ev"]["soc_kwh"]
    assert plant.read("modbus", "modbus.battery", "battery.soc").value > 50
    assert plant.invariant_violations() == []
