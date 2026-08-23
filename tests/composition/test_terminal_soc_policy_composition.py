from __future__ import annotations

import pytest

from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import (
    Constraint,
    Objective,
    OptimizationScenario,
    TerminalSOCPolicy,
    validate_scenario,
)
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.energy import energy_context_for


@pytest.mark.composition
def test_terminal_target_is_enforced_and_exposed_in_solver_evidence() -> None:
    context = energy_context_for(with_battery=True)
    scenario = OptimizationScenario(
        id="terminal-soc-composition-1",
        horizon=context.horizon,
        energy_context=context,
        terminal_soc_policy=TerminalSOCPolicy(
            minimum_kwh=3.0,
            target_kwh=4.0,
            value_eur_per_kwh=0.25,
        ),
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
        constraints=[Constraint(type="max_grid_import", value=10, unit="kW")],
    )

    result = CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    assert result.status in {
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL,
        OptimizationStatus.FEASIBLE,
    }
    assert result.constraint_summary["terminal_soc_policy"] == {
        "minimum_kwh": 3.0,
        "target_kwh": 4.0,
        "value_eur_per_kwh": 0.25,
    }
    assert result.objective_values["terminal_soc_kwh"] >= 4.0
    assert result.objective_values["terminal_soc_value_eur"] >= 1.0
    assert any("terminal_soc_value" in tier.terms for tier in result.solver_evidence.tiers)


@pytest.mark.composition
def test_terminal_soc_policy_without_battery_is_invalid_before_solver() -> None:
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="terminal-soc-composition-invalid-1",
        horizon=context.horizon,
        energy_context=context,
        terminal_soc_policy=TerminalSOCPolicy(minimum_kwh=1.0),
    )

    diagnostics = validate_scenario(scenario, DeviceRegistry())

    assert any(item.code == "terminal_soc_requires_battery" for item in diagnostics)
