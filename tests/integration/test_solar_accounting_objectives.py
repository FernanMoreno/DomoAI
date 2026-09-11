"""Enumerate every feasible tiny schedule, independently of CP-SAT's model."""

from itertools import product

import pytest

from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Objective
from tests.fixtures.energy import flexible_load
from tests.integration.test_energy_optimization import build_context, solar_accounting_scenario


def _all_outcomes(sun: tuple[int, ...], initial: int) -> list[tuple[int, int]]:
    # One tick is 1e-6 kW / 1e-6 kWh in one-hour slots. Distinct solar/other
    # pools suffice for an independent objective oracle; full three-origin
    # partition and losses are checked separately against literal examples.
    outcomes = []
    for start, choices in product(range(3), product(((0, 0), (1, 0), (0, 1)), repeat=3)):
        solar_stock, other_stock, self_ticks, cost_score = 0, initial, 0, 0
        for t, (charge, discharge) in enumerate(choices):
            total = solar_stock + other_stock
            if total + charge - discharge not in range(3):
                break
            local = int(t == start)
            direct = min(sun[t], local)
            solar_charge = min(sun[t] - direct, charge)
            solar_stock += solar_charge
            other_stock += charge - solar_charge
            net = local + charge - sun[t] - discharge
            imported, exported = max(0, net), max(0, -net)
            direct_export = sun[t] - direct - solar_charge
            battery_export = exported - direct_export
            solar_export = min(solar_stock, battery_export)
            solar_stock -= solar_export
            other_stock -= battery_export - solar_export
            solar_local = max(0, discharge - battery_export - other_stock)
            other_stock -= discharge - battery_export - solar_local
            solar_stock -= solar_local
            assert min(solar_stock, other_stock) >= 0
            self_ticks += direct + solar_local
            cost_score += imported * (1_000_000, 3_000_000, 2_000_000)[t]
            cost_score -= exported * (500_000, 2_000_000, 1_000_000)[t]
        else:
            outcomes.append((self_ticks, cost_score))
    assert outcomes
    return outcomes


@pytest.mark.parametrize("sun", [(0, 1, 0), (1, 0, 1), (0, 0, 0)])
@pytest.mark.parametrize("initial", [0, 1, 2])
@pytest.mark.parametrize("direction", ["maximize", "minimize"])
@pytest.mark.parametrize("tier_order", ["solar_first", "cost_first", "weighted"])
@pytest.mark.parametrize("solar_weight", [2, 1e-9], ids=["effective", "rounded_zero"])
async def test_solver_matches_exhaustive_objective_order(
    sun, initial, direction, tier_order, solar_weight,
):
    adapter, registry, service = await build_context()
    device = next(device.id for device in registry.devices if device.type.value == "switch")
    solar_priority = 1 if tier_order == "cost_first" else 0
    cost_priority = 1 if tier_order == "solar_first" else 0
    scenario = solar_accounting_scenario(
        initial=initial / 1_000_000, capacity=2 / 1_000_000,
        solar=tuple(value / 1_000_000 for value in sun), base=(0, 0, 0),
        max_charge=1 / 1_000_000, max_discharge=1 / 1_000_000,
        objectives=[
            Objective(name="maximize_solar_self_consumption", direction=direction,
                      priority=solar_priority, weight=solar_weight),
            Objective(name="minimize_energy_cost", direction="minimize", priority=cost_priority),
        ],
    )
    context = scenario.energy_context
    assert context is not None and context.export_tariffs is not None
    for point, price in zip(context.tariffs, (1, 3, 2), strict=True):
        point.price_per_kwh = price
    for point, price in zip(context.export_tariffs, (.5, 2, 1), strict=True):
        point.price_per_kwh = price
    scenario.loads = [flexible_load(device, power_kw=1e-6, latest_slot=2)]
    sign = -1 if direction == "maximize" else 1

    def score(outcome):
        self_ticks, cost = outcome
        solar = round(sign * solar_weight * 1_000_000) * self_ticks
        if tier_order == "weighted":
            return (solar + cost,)
        return (solar, cost) if tier_order == "solar_first" else (cost, solar)

    expected = min(score(outcome) for outcome in _all_outcomes(sun, initial))
    result = service.optimize(scenario)
    assert result.status is OptimizationStatus.OPTIMAL_HIERARCHY, result.diagnostics
    evidence = result.constraint_summary["solar_accounting"]
    actual = (
        round(evidence["objective_credit_kwh"] * 1_000_000),
        round(result.objective_values["energy_cost"] * 1_000_000_000_000),
    )
    assert score(actual) == expected
    assert evidence["local_quantization_remainder_kwh"] == 0
    assert result.objective_values["solar_self_consumption_kwh"] == pytest.approx(
        actual[0] / 1_000_000, abs=1e-15,
    )
    assert adapter.calls == []
