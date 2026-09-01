from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.dynamic_safety import DynamicSafetyGuard
from domoai.domain.energy import EVActuator
from domoai.domain.models import Command, SourceRef, StateSnapshot, StateStatus
from domoai.optimizer.energy import BatteryActuator, BatteryProfile
from domoai.runtime.clock import FixedClock
from domoai.runtime.state_store import StateStore


def _profile() -> BatteryProfile:
    return BatteryProfile(
        capacity_kwh=10,
        initial_soc_kwh=5,
        min_soc_kwh=2,
        max_soc_kwh=9,
        max_charge_kw=3,
        max_discharge_kw=3,
        charge_efficiency=1,
        discharge_efficiency=1,
        actuator=BatteryActuator(
            device_id="battery.one",
            capability="battery.control",
            charge_command="charge",
            discharge_command="discharge",
            stop_command="stop",
            power_feedback_capability="battery.power",
            power_feedback_tolerance_kw=0.1,
            soc_reconciliation_capability="battery.soc",
        ),
    )


def _snapshot(
    *,
    value: float,
    observed_at: datetime,
    status: StateStatus,
    received_at: datetime | None = None,
) -> StateSnapshot:
    return StateSnapshot(
        device_id="battery.one",
        capability="battery.soc",
        value=value,
        unit="kWh",
        observed_at=observed_at,
        received_at=received_at or observed_at,
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id="battery.soc"),
    )


def _command(command: str, value: float) -> Command:
    return Command(
        id=f"command-{command}",
        device_id="battery.one",
        command=command,
        value=value,
        unit="kW",
        idempotency_key=f"intent-{command}",
    )


def _ev_actuator() -> EVActuator:
    return EVActuator(
        device_id="ev.one",
        capability="ev.charge_power",
        charge_command="charge_ev",
        stop_command="stop_ev",
        max_charge_kw=7,
    )


def _ev_snapshot(
    capability: str,
    value: bool | float | str,
    *,
    observed_at: datetime,
    received_at: datetime | None = None,
) -> StateSnapshot:
    return StateSnapshot(
        device_id="ev.one",
        capability=capability,
        value=value,
        observed_at=observed_at,
        received_at=received_at or observed_at,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id=f"ev.one.{capability}"),
    )


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_discharge_at_current_reserve() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(clock=clock)
    await store.save(_snapshot(value=2, observed_at=now, status=StateStatus.CURRENT))
    guard = DynamicSafetyGuard(store, _profile(), clock=clock)

    error = await guard.check(_command("discharge", 0.5))

    assert error is not None
    assert error.code == "safety_limit_exceeded"
    assert "reserve" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_expired_soc_before_write() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await store.save(
        _snapshot(
            value=5,
            observed_at=now - timedelta(minutes=6),
            status=StateStatus.CURRENT,
        )
    )
    guard = DynamicSafetyGuard(store, _profile(), clock=clock)

    error = await guard.check(_command("charge", 0.5))

    assert error is not None
    assert error.code == "safety_limit_exceeded"
    assert "expired" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_uses_battery_receipt_age_not_source_observation_age() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    await store.save(
        _snapshot(
            value=5,
            observed_at=now - timedelta(hours=2),
            received_at=now - timedelta(seconds=1),
            status=StateStatus.CURRENT,
        )
    )
    guard = DynamicSafetyGuard(store, _profile(), clock=clock)

    error = await guard.check(_command("charge", 0.5))

    assert error is None


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_future_soc_before_write() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(clock=clock)
    await store.save(
        _snapshot(
            value=5,
            observed_at=now + timedelta(seconds=1),
            status=StateStatus.CURRENT,
        )
    )
    guard = DynamicSafetyGuard(store, _profile(), clock=clock)

    error = await guard.check(_command("charge", 0.5))

    assert error is not None
    assert error.code == "safety_limit_exceeded"
    assert "current" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_ev_charge_above_bound() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    store = StateStore(clock=FixedClock(now))
    await store.save(_ev_snapshot("ev.connected", True, observed_at=now))
    await store.save(
        _ev_snapshot("ev.departure_at", (now + timedelta(hours=2)).isoformat(), observed_at=now)
    )
    guard = DynamicSafetyGuard(
        store,
        None,
        ev_actuators=(_ev_actuator(),),
        clock=FixedClock(now),
    )

    error = await guard.check(
        Command(
            id="ev-charge",
            device_id="ev.one",
            command="charge_ev",
            value=7.1,
            unit="kW",
            idempotency_key="ev-charge-key",
        )
    )

    assert error is not None
    assert error.code == "safety_limit_exceeded"
    assert "envelope" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_uses_receipt_age_not_source_observation_age() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(stale_after=timedelta(minutes=5), clock=clock)
    observed_at = now - timedelta(hours=2)
    received_at = now - timedelta(seconds=1)
    await store.save(
        _ev_snapshot(
            "ev.connected",
            True,
            observed_at=observed_at,
            received_at=received_at,
        )
    )
    await store.save(
        _ev_snapshot(
            "ev.departure_at",
            (now + timedelta(hours=2)).isoformat(),
            observed_at=observed_at,
            received_at=received_at,
        )
    )
    guard = DynamicSafetyGuard(
        store,
        None,
        ev_actuators=(_ev_actuator(),),
        clock=clock,
    )

    error = await guard.check(
        Command(
            id="ev-charge-receipt-fresh",
            device_id="ev.one",
            command="charge_ev",
            value=1,
            unit="kW",
            idempotency_key="ev-charge-receipt-fresh-key",
        )
    )

    assert error is None


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_zero_ev_charge() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    store = StateStore(clock=FixedClock(now))
    await store.save(_ev_snapshot("ev.connected", True, observed_at=now))
    await store.save(
        _ev_snapshot("ev.departure_at", (now + timedelta(hours=2)).isoformat(), observed_at=now)
    )
    guard = DynamicSafetyGuard(
        store,
        None,
        ev_actuators=(_ev_actuator(),),
        clock=FixedClock(now),
    )

    error = await guard.check(
        Command(
            id="ev-zero-charge",
            device_id="ev.one",
            command="charge_ev",
            value=0,
            unit="kW",
            idempotency_key="ev-zero-charge-key",
        )
    )

    assert error is not None
    assert error.code == "safety_limit_exceeded"
    assert "envelope" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_ev_charge_when_disconnected_or_departed() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(clock=clock)
    await store.save(_ev_snapshot("ev.connected", False, observed_at=now))
    await store.save(
        _ev_snapshot("ev.departure_at", (now - timedelta(minutes=1)).isoformat(), observed_at=now)
    )
    guard = DynamicSafetyGuard(store, None, ev_actuators=(_ev_actuator(),), clock=clock)
    command = Command(
        id="ev-charge",
        device_id="ev.one",
        command="charge_ev",
        value=1,
        unit="kW",
        idempotency_key="ev-charge-key",
    )

    error = await guard.check(command)

    assert error is not None
    assert "connected" in error.message

    await store.save(_ev_snapshot("ev.connected", True, observed_at=now))
    error = await guard.check(command)

    assert error is not None
    assert "departure" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_rejects_future_ev_telemetry() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    store = StateStore(clock=clock)
    future = now + timedelta(seconds=1)
    await store.save(_ev_snapshot("ev.connected", True, observed_at=future))
    await store.save(
        _ev_snapshot("ev.departure_at", (now + timedelta(hours=2)).isoformat(), observed_at=now)
    )
    guard = DynamicSafetyGuard(store, None, ev_actuators=(_ev_actuator(),), clock=clock)

    error = await guard.check(
        Command(
            id="ev-charge-future",
            device_id="ev.one",
            command="charge_ev",
            value=1,
            unit="kW",
            idempotency_key="ev-charge-future-key",
        )
    )

    assert error is not None
    assert error.code == "safety_limit_exceeded"
    assert "current" in error.message


@pytest.mark.asyncio
async def test_dynamic_guard_allows_ev_stop_without_live_charge_state() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    guard = DynamicSafetyGuard(
        StateStore(clock=FixedClock(now)),
        None,
        ev_actuators=(_ev_actuator(),),
        clock=FixedClock(now),
    )

    error = await guard.check(
        Command(
            id="ev-stop",
            device_id="ev.one",
            command="stop_ev",
            value=0,
            unit="kW",
            idempotency_key="ev-stop-key",
        )
    )

    assert error is None
