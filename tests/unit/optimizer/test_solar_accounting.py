"""Exhaustive feasible physical slots must remain feasible with D1 attribution."""

from fractions import Fraction
from itertools import product

import pytest
from ortools.sat.python import cp_model

from domoai.optimizer.cp_sat import _add_solar_accounting_slot
from domoai.optimizer.solar_accounting import SolarAccountingConvention, accounting_evidence


@pytest.mark.parametrize("minutes", [1, 15, 60])
@pytest.mark.parametrize("gain,cost", [(1000, 1000), (900, 1250), (999, 1111), (0, 1000)])
def test_exhaustive_small_bus_balances_conserve_and_do_not_restrict_soc(minutes, gain, cost):
    # Unit-sized bus powers and quarter-kWh origin inventories. Only traces
    # satisfying the ORIGINAL exact SOC equality are considered feasible.
    p, e = 1_000_000, 1000
    checked = 0
    for stocks, solar, demand, charging, discharging in product(
        product((0, 250_000), repeat=3), (0, 1), (0, 1, 2), (0, 1), (0, 1),
    ):
        if charging and discharging:
            continue
        initial = tuple(stock * 60 * e for stock in stocks)
        terminal = sum(initial) + minutes * p * (gain * charging - cost * discharging)
        if terminal < 0 or terminal % (60 * e):
            continue
        net_export = solar + discharging - demand - charging
        exported, imported = max(0, net_export), max(0, -net_export)
        model = cp_model.CpModel()
        variables = _add_solar_accounting_slot(
            model, slot=0, solar=solar * p, local=demand * p, charge=charging * p,
            discharge=discharging * p, grid_export=exported * p, initial=initial,
            terminal_soc=terminal // (60 * e), origin_bound=max(sum(initial), terminal),
            max_charge=p, max_discharge=p, minutes=minutes, charge_gain=gain,
            discharge_cost=cost,
        )
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = 1
        assert solver.Solve(model) == cp_model.OPTIMAL
        flow = variables.solved(solver)
        assert sum(flow.terminal) == terminal
        assert sum(flow.local_draw) + sum(flow.export_draw) == minutes * p * cost * discharging
        # D1 adversarial bounds, independent of the sequential min implementation:
        # battery solar exported is the most it could export after direct PV;
        # battery solar local is the least needed after non-solar supply.
        battery_export = exported * p - flow.direct_export
        want_solar_export_draw = min(initial[0], minutes * cost * battery_export)
        assert flow.export_draw[0] == want_solar_export_draw
        remaining_other = sum(initial[1:]) - (minutes * cost * battery_export
                                              - want_solar_export_draw)
        want_solar_local_draw = max(
            0, minutes * cost * (discharging * p - battery_export) - remaining_other,
        )
        assert flow.local_draw[0] == want_solar_local_draw
        convention = SolarAccountingConvention(
            power_scale=p, soc_scale=p, efficiency_scale=e, resolution_minutes=minutes,
            charge_gain=gain, discharge_cost=cost, quantization_bound_kwh=minutes / (60 * p),
        )
        evidence = accounting_evidence([flow], convention, scope="slot")
        time = Fraction(minutes, 60)
        initial_energy = Fraction(sum(initial), 60 * e * p)
        terminal_energy = Fraction(terminal, 60 * e * p)
        losses = (
            charging * time * Fraction(e - gain, e)
            + discharging * time * Fraction(cost - e, e)
        )
        assert solar * time + imported * time + initial_energy == (
            demand * time + exported * time + losses + terminal_energy
        )
        for index, origin in enumerate(evidence.battery_origins):
            assert origin.initial_kwh + origin.charged_kwh == pytest.approx(
                origin.local_delivered_kwh + origin.exported_kwh + origin.charge_loss_kwh
                + origin.discharge_loss_kwh + origin.terminal_stored_kwh,
                abs=1e-12,
            )
            assert flow.initial[index] + minutes * gain * flow.charged[index] == (
                flow.terminal[index] + flow.local_draw[index] + flow.export_draw[index]
            )
        assert evidence.self_consumption_kwh == pytest.approx(
            evidence.objective_credit_kwh + evidence.local_quantization_remainder_kwh,
            abs=1e-12,
        )
        assert 0 <= evidence.local_quantization_remainder_kwh < minutes / (60 * p)
        checked += 1
    assert checked >= 48
