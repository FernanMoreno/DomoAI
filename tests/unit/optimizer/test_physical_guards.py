from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import domoai.optimizer.cp_sat as cp_sat
from domoai.domain.energy import BatteryProfile
from domoai.optimizer.energy import BaseLoadPoint, EnergyContext, SolarForecastPoint, TariffPoint
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Horizon, OptimizationScenario, validate_scenario
from domoai.runtime.registry import DeviceRegistry


def _horizon(*, slots: int) -> Horizon:
    start = datetime(2026, 8, 25, 12, tzinfo=UTC)
    return Horizon(
        start=start,
        end=start + timedelta(minutes=15 * slots),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )


def _battery_context(horizon: Horizon, *, base_load_kw: float) -> EnergyContext:
    return EnergyContext(
        horizon=horizon,
        tariffs=[TariffPoint(slot=0, price_per_kwh=0.1, currency="EUR")],
        solar_forecast=[SolarForecastPoint(slot=0, power=0.0)],
        base_load_forecast=[BaseLoadPoint(slot=0, power=base_load_kw)],
        battery=BatteryProfile(
            capacity_kwh=10.0,
            initial_soc_kwh=5.0,
            min_soc_kwh=0.0,
            max_soc_kwh=10.0,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=1.0,
            discharge_efficiency=1.0,
        ),
        source_revision="physical-guards-test",
        observed_at=datetime(2026, 8, 25, 11, tzinfo=UTC),
    )


def test_oversized_horizon_is_rejected_before_solver_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fail_if_solver_is_entered(_: OptimizationScenario) -> object:
        nonlocal called
        called = True
        raise AssertionError("solver must not receive an oversized horizon")

    monkeypatch.setattr(cp_sat, "solve_validated_scenario", fail_if_solver_is_entered)
    scenario = OptimizationScenario(id="oversized", horizon=_horizon(slots=10081))

    result = cp_sat.CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    assert result.status is OptimizationStatus.INVALID
    assert any(item.code == "horizon_too_large" for item in result.diagnostics)
    assert called is False


def test_hard_battery_reserve_applies_to_terminal_state() -> None:
    horizon = _horizon(slots=1)
    scenario = OptimizationScenario(
        id="terminal-reserve",
        horizon=horizon,
        energy_context=_battery_context(horizon, base_load_kw=10.0),
        constraints=[
            Constraint(type="max_grid_import", value=0.0, unit="kW"),
            Constraint(type="battery_min_soc", value=4.0, unit="kWh"),
        ],
    )

    result = cp_sat.CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE


def test_unverifiable_physical_constraint_fails_closed_before_solver() -> None:
    horizon = _horizon(slots=1)
    scenario = OptimizationScenario(
        id="unverifiable-physical-limit",
        horizon=horizon,
        constraints=[
            Constraint(
                type="max_grid_import",
                value=2.0,
                unit="kW",
                enforcement="physical_execution",
            )
        ],
    )

    errors = validate_scenario(scenario, DeviceRegistry())

    assert any(item.code == "physical_constraint_unverifiable" for item in errors)
