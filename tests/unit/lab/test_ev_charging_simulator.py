from datetime import UTC, datetime

import pytest

from domoai.lab.ev_charging_simulator import (
    EVChargingSimulationProfile,
    EVChargingSimulator,
)
from domoai.runtime.clock import FixedClock


def profile(**overrides: object) -> EVChargingSimulationProfile:
    payload: dict[str, object] = {
        "provider_id": "lab-ev-simulator",
        "device_id": "lab-ev-1",
        "capacity_kwh": 60.0,
        "initial_soc_kwh": 20.0,
        "max_charge_kw": 7.4,
        "charge_efficiency": 0.95,
        "initial_connected": True,
        "initial_departure_at": None,
        "tick_seconds": 1.0,
    }
    payload.update(overrides)
    return EVChargingSimulationProfile(**payload)


def test_charge_tick_raises_soc_with_efficiency_and_stops_at_capacity() -> None:
    simulator = EVChargingSimulator(
        profile(capacity_kwh=25.0, initial_soc_kwh=20.0),
        clock=FixedClock(datetime(2026, 8, 26, tzinfo=UTC)),
    )

    simulator.command("charge_ev", value=7.4, idempotency_key="charge-1")
    simulator.tick(3600)

    state = simulator.snapshot()
    assert state.mode == "idle"
    assert state.soc_kwh == pytest.approx(25.0)
    assert state.power_kw == 0


def test_charge_tick_applies_efficiency_below_capacity_limit() -> None:
    simulator = EVChargingSimulator(profile())

    simulator.command("charge_ev", value=7.4, idempotency_key="charge-1")
    simulator.tick(3600)

    state = simulator.snapshot()
    assert state.mode == "charging"
    assert state.soc_kwh == pytest.approx(20.0 + 7.4 * 0.95)


def test_stop_command_halts_charging_immediately() -> None:
    simulator = EVChargingSimulator(profile())

    simulator.command("charge_ev", value=7.4, idempotency_key="charge-1")
    simulator.command("stop_ev", idempotency_key="stop-1")

    state = simulator.snapshot()
    assert state.mode == "idle"
    assert state.power_kw == 0


def test_charge_rejects_value_above_max_charge_kw() -> None:
    simulator = EVChargingSimulator(profile())

    with pytest.raises(ValueError, match="max_charge_kw"):
        simulator.command("charge_ev", value=99.0, idempotency_key="too-large")


def test_charge_rejects_when_disconnected() -> None:
    simulator = EVChargingSimulator(profile(initial_connected=False))

    with pytest.raises(ValueError, match="connected"):
        simulator.command("charge_ev", value=3.0, idempotency_key="blocked")


def test_disconnecting_while_charging_auto_stops() -> None:
    simulator = EVChargingSimulator(profile())

    simulator.command("charge_ev", value=7.4, idempotency_key="charge-1")
    simulator.set_connected(False)

    state = simulator.snapshot()
    assert state.connected is False
    assert state.mode == "idle"
    assert state.power_kw == 0


def test_stop_is_idempotent_and_duplicate_command_does_not_reapply() -> None:
    simulator = EVChargingSimulator(profile())

    simulator.command("charge_ev", value=7.4, idempotency_key="same")
    simulator.command("charge_ev", value=1.0, idempotency_key="same")
    simulator.command("stop_ev", idempotency_key="stop")

    state = simulator.snapshot()
    assert state.mode == "idle"
    assert state.power_kw == 0


def test_fault_makes_feedback_unavailable_until_cleared() -> None:
    simulator = EVChargingSimulator(profile())

    simulator.set_fault("unavailable")

    state = simulator.snapshot()
    assert state.available is False
    assert state.fault == "unavailable"
    with pytest.raises(ConnectionError):
        simulator.command("charge_ev", value=1.0, idempotency_key="blocked")

    simulator.set_fault(None)
    assert simulator.snapshot().available is True


def test_set_departure_at_updates_snapshot() -> None:
    simulator = EVChargingSimulator(profile())
    departure = datetime(2026, 8, 27, 6, tzinfo=UTC)

    simulator.set_departure_at(departure)

    assert simulator.snapshot().departure_at == departure
