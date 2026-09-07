import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.energy import DispatchableBatteryBinding
from domoai.lab.battery_simulator import (
    BatterySimulationProfile,
    BatterySimulator,
)
from domoai.runtime.clock import FixedClock


def profile() -> BatterySimulationProfile:
    return BatterySimulationProfile(
        provider_id="lab-battery-simulator",
        device_id="lab-battery-1",
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=2.0,
        max_soc_kwh=9.0,
        max_charge_kw=4.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        tick_seconds=1.0,
    )


def test_charge_tick_updates_soc_and_power_with_efficiency() -> None:
    simulator = BatterySimulator(profile(), clock=FixedClock(datetime(2026, 8, 24, tzinfo=UTC)))

    simulator.command("charge_battery", value=2.0, idempotency_key="charge-1")
    simulator.tick(1800)

    state = simulator.snapshot()
    assert state.mode == "charging"
    assert state.power_kw == pytest.approx(2.0)
    assert state.soc_kwh == pytest.approx(5.9)
    assert state.available is True


def test_limits_reject_unsafe_power_and_keep_soc_inside_reserve() -> None:
    simulator = BatterySimulator(profile())

    with pytest.raises(ValueError, match="max_charge_kw"):
        simulator.command("charge_battery", value=4.1, idempotency_key="too-large")

    simulator.command("discharge_battery", value=3.0, idempotency_key="discharge-1")
    simulator.tick(3600)

    assert simulator.snapshot().soc_kwh == pytest.approx(2.0)
    with pytest.raises(ValueError, match="minimum SOC"):
        simulator.command("discharge_battery", value=1.0, idempotency_key="below-min")


def test_stop_is_idempotent_and_duplicate_command_does_not_reapply() -> None:
    simulator = BatterySimulator(profile())

    simulator.command("charge_battery", value=2.0, idempotency_key="same")
    simulator.command("discharge_battery", value=3.0, idempotency_key="same")
    simulator.command("stop_battery", idempotency_key="stop")

    state = simulator.snapshot()
    assert state.mode == "idle"
    assert state.power_kw == 0


def test_fault_makes_feedback_unavailable_until_cleared() -> None:
    simulator = BatterySimulator(profile())

    simulator.set_fault("unavailable")

    state = simulator.snapshot()
    assert state.available is False
    assert state.fault == "unavailable"
    with pytest.raises(ConnectionError):
        simulator.command("charge_battery", value=1.0, idempotency_key="blocked")

    simulator.set_fault(None)
    assert simulator.snapshot().available is True


def test_lab_profile_cannot_be_used_as_production_dispatch_binding() -> None:
    payload = json.loads(Path("dev/lab/battery/profile.json").read_text(encoding="utf-8"))

    with pytest.raises(ValueError):
        DispatchableBatteryBinding.model_validate(payload)
