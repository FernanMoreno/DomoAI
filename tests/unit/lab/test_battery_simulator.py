import ast
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


def test_battery_mqtt_reconnect_callback_republishes_discovery_and_state() -> None:
    source = Path("dev/lab/battery/server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    server_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "BatteryLabServer"
    )
    helper = next(
        node
        for node in server_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_handle_mqtt_connect"
    )
    start_mqtt = next(
        node
        for node in server_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "start_mqtt"
    )
    on_connect = next(
        node
        for node in ast.walk(start_mqtt)
        if isinstance(node, ast.FunctionDef) and node.name == "on_connect"
    )

    helper_calls = {
        call.func.attr
        for call in ast.walk(helper)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }
    callback_calls = {
        call.func.attr
        for call in ast.walk(on_connect)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }
    current_state = next(
        node
        for node in server_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_publish_current_state"
    )
    current_state_calls = {
        call.func.attr
        for call in ast.walk(current_state)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }
    helper_source = ast.get_source_segment(source, helper)
    assert helper_source is not None
    assert "publish_discovery" in helper_calls
    assert "self._publish_current_state" in helper_source
    assert "publish" in current_state_calls
    assert "_handle_mqtt_connect" in callback_calls
