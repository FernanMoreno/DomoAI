from datetime import UTC, datetime

import pytest

from domoai.lab.thermal_simulator import ThermalSimulationProfile, ThermalSimulator
from domoai.runtime.clock import FixedClock


def profile(**overrides: object) -> ThermalSimulationProfile:
    payload: dict[str, object] = {
        "provider_id": "lab-thermostat",
        "device_id": "lab-thermostat-1",
        "capacitance_kwh_per_c": 0.5,
        "ua_kw_per_c": 0.05,
        "initial_temperature_c": 20.0,
        "initial_exterior_temperature_c": 10.0,
        "max_heat_kw": 2.0,
        "max_cool_kw": 2.0,
        "heating_cop": 3.0,
        "cooling_cop": 2.5,
        "tick_seconds": 60.0,
    }
    payload.update(overrides)
    return ThermalSimulationProfile(**payload)


def test_heat_mode_raises_temperature_measurably_and_never_instantaneously() -> None:
    simulator = ThermalSimulator(profile())

    before = simulator.snapshot().indoor_temperature_c
    simulator.set_hvac_mode("heat")
    after_set = simulator.snapshot().indoor_temperature_c
    simulator.tick()
    after_tick = simulator.snapshot().indoor_temperature_c

    assert after_set == pytest.approx(before)  # no change until a tick elapses
    assert after_tick > after_set


def test_cool_mode_lowers_temperature() -> None:
    simulator = ThermalSimulator(profile(initial_temperature_c=25.0))

    simulator.set_hvac_mode("cool")
    simulator.tick()

    assert simulator.snapshot().indoor_temperature_c < 25.0


def test_off_mode_drifts_toward_exterior_temperature() -> None:
    simulator = ThermalSimulator(
        profile(initial_temperature_c=20.0, initial_exterior_temperature_c=10.0)
    )

    simulator.set_hvac_mode("off")
    for _ in range(10):
        simulator.tick()

    state = simulator.snapshot()
    assert state.indoor_temperature_c < 20.0
    assert state.indoor_temperature_c > 10.0
    assert state.hvac_power_kw == pytest.approx(0.0)


def test_set_exterior_temperature_changes_the_passive_drift_target() -> None:
    simulator = ThermalSimulator(profile(initial_temperature_c=20.0))
    simulator.set_hvac_mode("off")

    simulator.set_exterior_temperature(30.0)
    simulator.tick()

    assert simulator.snapshot().indoor_temperature_c > 20.0


def test_fault_unavailable_blocks_reads_and_clears_cleanly() -> None:
    simulator = ThermalSimulator(profile())

    simulator.set_fault("unavailable")
    state = simulator.snapshot()
    assert state.available is False
    assert state.fault == "unavailable"
    with pytest.raises(ConnectionError):
        simulator.tick()

    simulator.set_fault(None)
    resumed = simulator.snapshot()
    assert resumed.available is True
    assert resumed.fault is None
    # No residual "stuck" state: a tick right after clearing must succeed.
    simulator.tick()


def test_large_number_of_ticks_accumulates_without_precision_loss_or_overflow() -> None:
    simulator = ThermalSimulator(profile(tick_seconds=60.0))
    simulator.set_hvac_mode("off")

    for _ in range(10_000):
        simulator.tick()

    state = simulator.snapshot()
    # Converges toward exterior temperature (10.0) and stays there --
    # never overflows or diverges over a long simulated duration.
    assert 9.9 <= state.indoor_temperature_c <= 20.0


def test_snapshot_uses_injected_clock_for_observed_at() -> None:
    fixed = datetime(2026, 8, 28, 12, tzinfo=UTC)
    simulator = ThermalSimulator(profile(), clock=FixedClock(fixed))

    assert simulator.snapshot().observed_at == fixed
