"""Confirms tiered optimization enforces a total time budget and reports
hierarchy-quality status honestly (Spec 083)."""

from __future__ import annotations

import itertools

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.optimizer.cp_sat import CpSatOptimizer, _status_for_tiers
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, flexible_load


def test_status_for_tiers_reports_optimal_hierarchy_only_when_not_degraded() -> None:
    from ortools.sat.python import cp_model

    assert _status_for_tiers(int(cp_model.OPTIMAL), degraded=False) is (
        OptimizationStatus.OPTIMAL_HIERARCHY
    )


def test_status_for_tiers_reports_feasible_hierarchy_when_degraded() -> None:
    from ortools.sat.python import cp_model

    assert _status_for_tiers(int(cp_model.OPTIMAL), degraded=True) is (
        OptimizationStatus.FEASIBLE_HIERARCHY
    )
    assert _status_for_tiers(int(cp_model.FEASIBLE), degraded=True) is (
        OptimizationStatus.FEASIBLE_HIERARCHY
    )


async def _build_registry() -> tuple[DeviceRegistry, str]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    return registry, device_id


def _multi_tier_scenario(device_id: str, *, time_limit: float) -> OptimizationScenario:
    context = energy_context_for()
    return OptimizationScenario(
        id="tier-budget-test",
        horizon=context.horizon,
        energy_context=context,
        solver_time_limit_seconds=time_limit,
        loads=[flexible_load(device_id, power_kw=1.5, latest_slot=5)],
        constraints=[
            Constraint(type="max_house_power", value=3, unit="kW"),
            Constraint(type="max_grid_import", value=3, unit="kW"),
            Constraint(type="max_grid_export", value=3, unit="kW"),
        ],
        objectives=[
            Objective(name="minimize_energy_cost", direction="minimize", priority=0),
            Objective(name="maximize_solar_self_consumption", direction="maximize", priority=1),
            Objective(name="minimize_start", direction="minimize", priority=2),
        ],
    )


@pytest.mark.asyncio
async def test_budget_exhaustion_stops_the_hierarchy_early_and_reports_feasible_hierarchy() -> None:
    registry, device_id = await _build_registry()
    scenario = _multi_tier_scenario(device_id, time_limit=5.0)

    # Simulate the total budget being consumed after the first tier's solve:
    # first two calls establish `start` and the first tier's `remaining`
    # (both near t=0), every call after that reports the budget as spent.
    monotonic_values = itertools.chain([0.0, 0.1], itertools.repeat(100.0))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "domoai.optimizer.cp_sat.time.monotonic", lambda: next(monotonic_values)
        )
        result = CpSatOptimizer(registry).optimize(scenario)

    assert result.solver_evidence is not None
    # At least the soft-violation/first tier solved, but not the full
    # declared hierarchy (3 objective tiers + implicit tiers) -- proves the
    # loop stopped instead of continuing to consume budget per tier.
    assert 0 < len(result.solver_evidence.tiers) < 3
    assert result.status is OptimizationStatus.FEASIBLE_HIERARCHY


@pytest.mark.asyncio
async def test_full_budget_produces_optimal_hierarchy_when_every_tier_solves_cleanly() -> None:
    registry, device_id = await _build_registry()
    scenario = _multi_tier_scenario(device_id, time_limit=5.0)

    result = CpSatOptimizer(registry).optimize(scenario)

    assert result.status is OptimizationStatus.OPTIMAL_HIERARCHY
