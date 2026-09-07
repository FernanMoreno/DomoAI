from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.dynamic_safety import DynamicSafetyGuard
from domoai.domain.energy import BatteryActuator, BatteryProfile, EVChargingBinding
from domoai.domain.models import Command, ScalarValue, SourceRef, StateSnapshot, StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.state_store import StateStore


def _profile() -> BatteryProfile:
    return BatteryProfile(
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=2.0,
        max_soc_kwh=9.0,
        max_charge_kw=4.0,
        max_discharge_kw=4.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        actuator=BatteryActuator(
            device_id="battery.home",
            capability="battery.power",
            charge_command="charge",
            discharge_command="discharge",
            stop_command="stop",
            power_feedback_capability="battery.power",
            power_feedback_tolerance_kw=0.1,
            soc_reconciliation_capability="battery.soc",
        ),
    )


def _snapshot(
    capability: str,
    value: ScalarValue,
    *,
    clock: FixedClock,
    status: StateStatus = StateStatus.CURRENT,
    age: timedelta = timedelta(seconds=0),
) -> StateSnapshot:
    observed_at = clock.now() - age
    return StateSnapshot(
        device_id="battery.home",
        capability=capability,
        value=value,
        observed_at=observed_at,
        received_at=clock.now(),
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id=capability),
    )


def _command(name: str, value: float) -> Command:
    return Command(
        id=f"battery-{name}",
        device_id="battery.home",
        command=name,
        value=value,
        unit="kW",
        idempotency_key=f"battery-{name}",
    )


def _ev_binding() -> EVChargingBinding:
    return EVChargingBinding(
        provider_id="fixture_ev",
        device_id="ev.garage",
        capability="ev.charge_power",
        charge_command="set_charge_power",
        stop_command="stop_charging",
        connected_capability="ev.connected",
        soc_capability="ev.soc",
        power_feedback_capability="ev.power",
        departure_capability="ev.departure_at",
        capacity_kwh=60.0,
        max_charge_kw=7.4,
    )


def _ev_snapshot(capability: str, value: ScalarValue, *, clock: FixedClock) -> StateSnapshot:
    return StateSnapshot(
        device_id="ev.garage",
        capability=capability,
        value=value,
        observed_at=clock.now(),
        received_at=clock.now(),
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture_ev", external_id=capability),
    )


@pytest.mark.asyncio
async def test_battery_dispatch_requires_current_soc_and_power_readbacks() -> None:
    clock = FixedClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    state_store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await state_store.save(_snapshot("battery.soc", 5.0, clock=clock))
    guard = DynamicSafetyGuard(state_store, _profile(), clock=clock)

    error = await guard.check(_command("charge", 2.0))

    assert error is not None
    assert "power" in error.message.lower()


@pytest.mark.asyncio
async def test_battery_dispatch_rejects_current_power_outside_profile_envelope() -> None:
    clock = FixedClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    state_store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await state_store.save(_snapshot("battery.soc", 5.0, clock=clock))
    await state_store.save(_snapshot("battery.power", 5.0, clock=clock))
    guard = DynamicSafetyGuard(state_store, _profile(), clock=clock)

    error = await guard.check(_command("charge", 2.0))

    assert error is not None
    assert "envelope" in error.message.lower()


@pytest.mark.asyncio
async def test_battery_dispatch_accepts_current_state_inside_profile_envelope() -> None:
    clock = FixedClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    state_store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await state_store.save(_snapshot("battery.soc", 5.0, clock=clock))
    await state_store.save(_snapshot("battery.power", 0.0, clock=clock))
    guard = DynamicSafetyGuard(state_store, _profile(), clock=clock)

    assert await guard.check(_command("charge", 2.0)) is None


@pytest.mark.asyncio
async def test_ev_charge_requires_current_connection_soc_power_and_departure() -> None:
    clock = FixedClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    state_store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await state_store.save(_ev_snapshot("ev.connected", True, clock=clock))
    await state_store.save(_ev_snapshot("ev.soc", 20.0, clock=clock))
    await state_store.save(_ev_snapshot("ev.power", 0.0, clock=clock))
    await state_store.save(
        _ev_snapshot("ev.departure_at", "2026-08-25T13:00:00+00:00", clock=clock)
    )
    guard = DynamicSafetyGuard(
        state_store, _profile(), ev_bindings=[_ev_binding()], clock=clock
    )
    command = Command(
        id="ev-charge",
        device_id="ev.garage",
        command="set_charge_power",
        value=7.0,
        unit="kW",
        idempotency_key="ev-charge",
    )

    assert await guard.check(command) is None


@pytest.mark.asyncio
async def test_ev_charge_rejects_disconnected_vehicle() -> None:
    clock = FixedClock(datetime(2026, 8, 25, 12, tzinfo=UTC))
    state_store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await state_store.save(_ev_snapshot("ev.connected", False, clock=clock))
    await state_store.save(_ev_snapshot("ev.soc", 20.0, clock=clock))
    await state_store.save(_ev_snapshot("ev.power", 0.0, clock=clock))
    await state_store.save(
        _ev_snapshot("ev.departure_at", "2026-08-25T13:00:00+00:00", clock=clock)
    )
    guard = DynamicSafetyGuard(
        state_store, _profile(), ev_bindings=[_ev_binding()], clock=clock
    )
    command = Command(
        id="ev-charge-disconnected",
        device_id="ev.garage",
        command="set_charge_power",
        value=7.0,
        unit="kW",
        idempotency_key="ev-charge-disconnected",
    )

    error = await guard.check(command)

    assert error is not None
    assert "connected" in error.message.lower()


@pytest.mark.asyncio
async def test_ev_charge_rejects_expired_departure_and_excess_power() -> None:
    clock = FixedClock(datetime(2026, 8, 25, 14, tzinfo=UTC))
    state_store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await state_store.save(_ev_snapshot("ev.connected", True, clock=clock))
    await state_store.save(_ev_snapshot("ev.soc", 20.0, clock=clock))
    await state_store.save(_ev_snapshot("ev.power", 8.0, clock=clock))
    await state_store.save(
        _ev_snapshot("ev.departure_at", "2026-08-25T13:00:00+00:00", clock=clock)
    )
    guard = DynamicSafetyGuard(
        state_store, _profile(), ev_bindings=[_ev_binding()], clock=clock
    )
    command = Command(
        id="ev-charge-expired",
        device_id="ev.garage",
        command="set_charge_power",
        value=8.0,
        unit="kW",
        idempotency_key="ev-charge-expired",
    )

    error = await guard.check(command)

    assert error is not None
    assert "departure" in error.message.lower() or "power" in error.message.lower()
