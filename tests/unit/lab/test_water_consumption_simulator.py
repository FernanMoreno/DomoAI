from datetime import UTC, datetime

import pytest

from domoai.lab.water_consumption_simulator import (
    WaterConsumptionSimulationProfile,
    WaterConsumptionSimulator,
)
from domoai.runtime.clock import FixedClock


def profile(**overrides: object) -> WaterConsumptionSimulationProfile:
    payload: dict[str, object] = {
        "provider_id": "lab-water-meter",
        "device_id": "lab-water-1",
        "initial_total_volume_l": 0.0,
        "initial_flow_rate_lpm": 0.0,
        "tick_seconds": 1.0,
    }
    payload.update(overrides)
    return WaterConsumptionSimulationProfile(**payload)


def test_flow_and_tick_accumulate_total_volume_exactly() -> None:
    simulator = WaterConsumptionSimulator(
        profile(), clock=FixedClock(datetime(2026, 8, 28, tzinfo=UTC))
    )

    simulator.set_flow_rate(6.0)
    simulator.tick(600)  # 10 minutes

    state = simulator.snapshot()
    assert state.flow_rate_lpm == pytest.approx(6.0)
    assert state.total_volume_l == pytest.approx(60.0)


def test_total_volume_never_decreases_across_ticks() -> None:
    simulator = WaterConsumptionSimulator(profile())

    simulator.set_flow_rate(2.0)
    simulator.tick(60)
    first_total = simulator.snapshot().total_volume_l
    simulator.set_flow_rate(0.0)
    simulator.tick(60)

    assert simulator.snapshot().total_volume_l == pytest.approx(first_total)
    assert simulator.snapshot().total_volume_l >= first_total


def test_zero_flow_reads_as_zero_and_available() -> None:
    simulator = WaterConsumptionSimulator(profile())

    simulator.set_flow_rate(0.0)
    state = simulator.snapshot()

    assert state.flow_rate_lpm == 0.0
    assert state.available is True


def test_fault_unavailable_blocks_reads_and_clears_cleanly() -> None:
    simulator = WaterConsumptionSimulator(profile())

    simulator.set_fault("unavailable")
    state = simulator.snapshot()
    assert state.available is False
    assert state.fault == "unavailable"
    with pytest.raises(ConnectionError):
        simulator.tick(60)

    simulator.set_fault(None)
    resumed = simulator.snapshot()
    assert resumed.available is True
    assert resumed.fault is None
    # No residual "stuck" state: a tick right after clearing must succeed normally.
    simulator.set_flow_rate(1.0)
    simulator.tick(60)
    assert simulator.snapshot().total_volume_l == pytest.approx(1.0)


def test_set_flow_rate_rejects_negative_value() -> None:
    simulator = WaterConsumptionSimulator(profile())

    with pytest.raises(ValueError, match="negative"):
        simulator.set_flow_rate(-1.0)


def test_leak_condition_is_observable_through_the_ordinary_read_path() -> None:
    # Spec 163 User Story 3, Scenario 2 / FR-007: a leak is simply a
    # sustained nonzero flow rate with no corresponding legitimate use --
    # the simulator has no separate "leak" mechanism, and the point of this
    # test is to prove that's sufficient: the ordinary flow_rate_lpm/
    # total_volume_l reading already makes it observable, nothing hidden.
    simulator = WaterConsumptionSimulator(profile())

    simulator.set_flow_rate(2.0)  # continuous, unexplained flow
    simulator.tick(3600)  # one hour with no legitimate use ending it

    state = simulator.snapshot()
    assert state.flow_rate_lpm == pytest.approx(2.0)
    assert state.total_volume_l > 0


def test_large_number_of_ticks_accumulates_without_precision_loss() -> None:
    # Spec 163 analysis finding E2 / spec Edge Case 1.
    simulator = WaterConsumptionSimulator(profile())
    simulator.set_flow_rate(1.0)

    for _ in range(10_000):
        simulator.tick(60)  # 10,000 minutes of flow at 1 L/min

    assert simulator.snapshot().total_volume_l == pytest.approx(10_000.0, rel=1e-9)
