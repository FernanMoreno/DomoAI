"""Integer-scaled CP-SAT translation for proposal-only scheduling."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from typing import Any

import ortools
from ortools.sat.python import cp_model

from domoai.domain.models import Command, CommandPostcondition, Plan
from domoai.optimizer.ports import (
    OptimizationResult,
    OptimizationStatus,
    SolvedTier,
    SolverEvidence,
    build_result,
)
from domoai.optimizer.scenario import Objective, OptimizationScenario, validate_scenario
from domoai.runtime.registry import DeviceRegistry

POWER_SCALE = 1_000_000  # mW when the public unit is kW
SOC_SCALE = 1_000_000  # mWh when the public unit is kWh
EFFICIENCY_SCALE = 1_000
OBJECTIVE_SCALE = 1_000_000

_SOLVER_VERSION: str = ortools.__version__  # type: ignore[attr-defined]


class CpSatOptimizer:
    def __init__(self, registry: DeviceRegistry) -> None:
        self.registry = registry

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        diagnostics = validate_scenario(scenario, self.registry)
        if diagnostics:
            return OptimizationResult(
                scenario_id=scenario.id,
                status=OptimizationStatus.INVALID,
                solver="cp-sat",
                diagnostics=diagnostics,
            )
        if scenario.solver_time_limit_seconds == 0:
            return build_result(
                scenario_id=scenario.id,
                status=OptimizationStatus.TIMEOUT,
                diagnostics=[{"code": "timeout", "message": "Solver budget is zero"}],
            )
        if scenario.energy_context is not None:
            return self._optimize_energy(scenario)
        return self._optimize_legacy(scenario)

    def _optimize_legacy(self, scenario: OptimizationScenario) -> OptimizationResult:
        model: Any = cp_model.CpModel()
        start_variables = _start_variables(model, scenario)
        horizon_slots = scenario.horizon.slots

        for constraint in scenario.constraints:
            if constraint.type != "max_house_power" or not constraint.hard:
                continue
            limit = to_solver_int(constraint.value, constraint.unit)
            for slot in range(horizon_slots):
                active_terms = _active_load_terms(scenario, start_variables, slot)
                model.Add(sum(active_terms) <= limit)

        objective_terms: list[Any] = []
        objectives = sorted(scenario.objectives, key=lambda item: (item.priority, item.name))
        for load in scenario.loads:
            for start, variable in start_variables[load.id].items():
                weight = _objective_weight(objectives, "minimize_start")
                if weight:
                    objective_terms.append(int(weight * start) * variable)
        if objective_terms:
            model.Minimize(sum(objective_terms))

        solver, status = _solve(model, scenario, time_limit=scenario.solver_time_limit_seconds)
        failure = _failure_result(scenario, status)
        if failure is not None:
            return failure

        selected_slots = _selected_slots(solver, start_variables)
        plans = _proposal_plan(scenario, selected_slots)
        minimize_start_objective = next(
            (objective for objective in objectives if objective.name == "minimize_start"), None
        )
        solver_evidence = SolverEvidence(
            solver_name="cp-sat",
            solver_version=_SOLVER_VERSION,
            num_search_workers=solver.parameters.num_search_workers,
            random_seed=solver.parameters.random_seed,
            wall_time_seconds=solver.WallTime(),
            tiers=[
                SolvedTier(
                    priority=(
                        minimize_start_objective.priority if minimize_start_objective else None
                    ),
                    terms=["minimize_start"] if objective_terms else [],
                    achieved_value=(solver.Value(sum(objective_terms)) if objective_terms else 0),
                )
            ],
            scenario_fingerprint=_scenario_fingerprint(scenario),
        )
        return build_result(
            scenario_id=scenario.id,
            status=(OptimizationStatus.NO_ACTION_REQUIRED if not plans else _status(status)),
            plan=plans[0] if plans else None,
            plans=plans,
            objective_values={"start_slot_sum": float(sum(selected_slots.values()))},
            constraint_summary={"hard_satisfied": True, "soft_violations": []},
            solver_evidence=solver_evidence,
        )

    def _optimize_energy(self, scenario: OptimizationScenario) -> OptimizationResult:
        context = scenario.energy_context
        assert context is not None
        model: Any = cp_model.CpModel()
        start_variables = _start_variables(model, scenario)
        horizon_slots = scenario.horizon.slots
        load_powers = {
            load.id: _load_power(load, scenario.horizon.resolution_minutes)
            for load in scenario.loads
        }
        solar_powers = [
            to_solver_int(
                point.confidence.low
                if scenario.conservative and point.confidence is not None
                else point.power,
                point.unit,
            )
            for point in context.solar_forecast
        ]
        base_load_powers = (
            [
                to_solver_int(
                    point.confidence.high
                    if scenario.conservative and point.confidence is not None
                    else point.power,
                    point.unit,
                )
                for point in context.base_load_forecast
            ]
            if context.base_load_forecast is not None
            else [0] * horizon_slots
        )
        forecast_confidence = {
            "solar_bounded": all(point.confidence is not None for point in context.solar_forecast),
            "base_load_bounded": (
                all(point.confidence is not None for point in context.base_load_forecast)
                if context.base_load_forecast is not None
                else None
            ),
        }

        battery = context.battery
        charge_variables: list[Any] = []
        discharge_variables: list[Any] = []
        soc_variables: list[Any] = []
        if battery is not None:
            max_charge = to_solver_int(battery.max_charge_kw, "kW")
            max_discharge = to_solver_int(battery.max_discharge_kw, "kW")
            min_soc = to_energy_int(battery.min_soc_kwh)
            max_soc = to_energy_int(battery.max_soc_kwh)
            soc_variables = [
                model.NewIntVar(min_soc, max_soc, f"battery_soc_{slot}")
                for slot in range(horizon_slots + 1)
            ]
            model.Add(soc_variables[0] == to_energy_int(battery.initial_soc_kwh))
            charge_gain = round(battery.charge_efficiency * EFFICIENCY_SCALE)
            discharge_cost = round(EFFICIENCY_SCALE / battery.discharge_efficiency)
        else:
            max_charge = 0
            max_discharge = 0
            charge_gain = EFFICIENCY_SCALE
            discharge_cost = EFFICIENCY_SCALE

        ev_soc_variables: dict[str, list[Any]] = {}
        ev_charge_variables: dict[str, list[Any]] = {}
        for ev_load in scenario.ev_loads:
            ev_capacity = to_energy_int(ev_load.capacity_kwh)
            soc_vars = [
                model.NewIntVar(0, ev_capacity, f"ev_soc_{ev_load.id}_{slot}")
                for slot in range(horizon_slots + 1)
            ]
            model.Add(soc_vars[0] == to_energy_int(ev_load.initial_soc_kwh))
            ev_soc_variables[ev_load.id] = soc_vars
            ev_charge_variables[ev_load.id] = []

        comfort_active_variables: dict[str, dict[int, Any]] = {}
        for comfort_load in scenario.comfort_loads:
            variables = {
                slot: model.NewBoolVar(f"comfort_{comfort_load.id}_{slot}")
                for slot in range(comfort_load.earliest_slot, comfort_load.deadline_slot)
            }
            model.Add(sum(variables.values()) >= comfort_load.min_active_slots)
            comfort_active_variables[comfort_load.id] = variables

        ev_charge_bound = sum(
            to_solver_int(ev_load.max_charge_kw, "kW") for ev_load in scenario.ev_loads
        )
        comfort_power_bound = sum(
            to_solver_int(comfort_load.power, comfort_load.power_unit)
            for comfort_load in scenario.comfort_loads
        )
        load_bound = (
            sum(load_powers.values())
            + max(base_load_powers, default=0)
            + ev_charge_bound
            + comfort_power_bound
        )
        solar_bound = max(solar_powers, default=0)
        grid_bound = max(1, load_bound + solar_bound + max_charge + max_discharge) * 2
        for constraint in scenario.constraints:
            if constraint.type in {"max_grid_import", "max_grid_export"}:
                grid_bound = max(grid_bound, to_solver_int(constraint.value, constraint.unit))

        grid_import: list[Any] = []
        grid_export: list[Any] = []
        peak_import = model.NewIntVar(0, grid_bound, "peak_grid_import")
        soft_violations: list[tuple[str, int, Any]] = []
        for slot in range(horizon_slots):
            importing = model.NewBoolVar(f"grid_importing_{slot}")
            grid_in = model.NewIntVar(0, grid_bound, f"grid_import_{slot}")
            grid_out = model.NewIntVar(0, grid_bound, f"grid_export_{slot}")
            grid_import.append(grid_in)
            grid_export.append(grid_out)
            model.Add(grid_in == 0).OnlyEnforceIf(importing.Not())
            model.Add(grid_out == 0).OnlyEnforceIf(importing)
            model.Add(peak_import >= grid_in)

            if battery is not None:
                charge = model.NewIntVar(0, max_charge, f"battery_charge_{slot}")
                discharge = model.NewIntVar(0, max_discharge, f"battery_discharge_{slot}")
                charging = model.NewBoolVar(f"battery_charging_{slot}")
                model.Add(charge == 0).OnlyEnforceIf(charging.Not())
                model.Add(discharge == 0).OnlyEnforceIf(charging)
                charge_variables.append(charge)
                discharge_variables.append(discharge)
                model.Add(
                    soc_variables[slot + 1] * EFFICIENCY_SCALE * 60
                    == soc_variables[slot] * EFFICIENCY_SCALE * 60
                    + charge * scenario.horizon.resolution_minutes * charge_gain
                    - discharge * scenario.horizon.resolution_minutes * discharge_cost
                )
            else:
                charge = 0
                discharge = 0

            for ev_load in scenario.ev_loads:
                max_ev_charge = to_solver_int(ev_load.max_charge_kw, "kW")
                min_ev_charge = to_solver_int(ev_load.min_charge_kw, "kW")
                ev_charge = model.NewIntVar(0, max_ev_charge, f"ev_charge_{ev_load.id}_{slot}")
                if slot >= ev_load.deadline_slot:
                    model.Add(ev_charge == 0)
                if min_ev_charge > 0:
                    ev_charging = model.NewBoolVar(f"ev_charging_{ev_load.id}_{slot}")
                    model.Add(ev_charge == 0).OnlyEnforceIf(ev_charging.Not())
                    model.Add(ev_charge >= min_ev_charge).OnlyEnforceIf(ev_charging)
                ev_charge_variables[ev_load.id].append(ev_charge)
                soc_vars = ev_soc_variables[ev_load.id]
                ev_charge_gain = round(ev_load.charge_efficiency * EFFICIENCY_SCALE)
                model.Add(
                    soc_vars[slot + 1] * EFFICIENCY_SCALE * 60
                    == soc_vars[slot] * EFFICIENCY_SCALE * 60
                    + ev_charge * scenario.horizon.resolution_minutes * ev_charge_gain
                )

            ev_charge_total = sum(
                ev_charge_variables[ev_load.id][slot] for ev_load in scenario.ev_loads
            )
            comfort_total = sum(
                to_solver_int(comfort_load.power, comfort_load.power_unit)
                * comfort_active_variables[comfort_load.id][slot]
                for comfort_load in scenario.comfort_loads
                if slot in comfort_active_variables[comfort_load.id]
            )
            active_load = (
                sum(_active_load_terms(scenario, start_variables, slot, load_powers))
                + ev_charge_total
                + comfort_total
            )
            model.Add(
                active_load + charge + base_load_powers[slot]
                == solar_powers[slot] + grid_in + discharge - grid_out
            )
            _add_energy_constraints(
                model,
                scenario,
                slot,
                active_load + base_load_powers[slot],
                charge,
                grid_in,
                grid_out,
                soc_variables,
                grid_bound,
                soft_violations,
            )

        for ev_load in scenario.ev_loads:
            model.Add(
                ev_soc_variables[ev_load.id][ev_load.deadline_slot]
                >= to_energy_int(ev_load.target_soc_kwh)
            )

        terminal_policy = scenario.terminal_soc_policy
        if battery is not None and terminal_policy is not None:
            if terminal_policy.minimum_kwh is not None:
                model.Add(soc_variables[-1] >= to_energy_int(terminal_policy.minimum_kwh))
            if terminal_policy.target_kwh is not None:
                model.Add(soc_variables[-1] >= to_energy_int(terminal_policy.target_kwh))

        solver, status, solved_tiers, wall_time, degraded = _solve_tiers(
            model,
            scenario,
            start_variables,
            grid_import,
            grid_export,
            peak_import,
            soft_violations,
            resolution_hours=scenario.horizon.resolution_minutes / 60,
            context=context,
            charge_variables=charge_variables,
            discharge_variables=discharge_variables,
            terminal_soc_variable=(soc_variables[-1] if battery is not None else None),
        )
        failure = _failure_result(scenario, status)
        if failure is not None:
            return failure

        selected_slots = _selected_slots(solver, start_variables)
        ev_charge_slots: dict[str, dict[int, float]] = {
            ev_load.id: {
                slot: solver.Value(variable) / POWER_SCALE
                for slot, variable in enumerate(ev_charge_variables[ev_load.id])
                if solver.Value(variable) > 0
            }
            for ev_load in scenario.ev_loads
        }
        comfort_active_slots: dict[str, list[int]] = {
            comfort_load.id: [
                slot
                for slot, variable in comfort_active_variables[comfort_load.id].items()
                if solver.Value(variable)
            ]
            for comfort_load in scenario.comfort_loads
        }
        battery_dispatch_slots: dict[int, tuple[float, float]] | None = None
        if battery is not None:
            battery_dispatch_slots = {
                slot: (
                    solver.Value(charge_variables[slot]) / POWER_SCALE,
                    solver.Value(discharge_variables[slot]) / POWER_SCALE,
                )
                for slot in range(horizon_slots)
            }
        plans = _proposal_plan(
            scenario,
            selected_slots,
            ev_charge_slots,
            comfort_active_slots,
            battery_dispatch_slots,
        )
        slots: list[dict[str, float | int]] = []
        energy_cost = 0.0
        export_revenue = 0.0
        export_kwh = 0.0
        solar_kwh = 0.0
        battery_throughput_kwh = 0.0
        resolution_hours = scenario.horizon.resolution_minutes / 60
        for slot in range(horizon_slots):
            load_power = sum(
                load_powers[load.id]
                for load in scenario.loads
                if any(
                    start <= slot < start + load.duration_slots and solver.Value(variable)
                    for start, variable in start_variables[load.id].items()
                )
            )
            solar_kw = solar_powers[slot] / POWER_SCALE
            import_kw = solver.Value(grid_import[slot]) / POWER_SCALE
            export_kw = solver.Value(grid_export[slot]) / POWER_SCALE
            charge_kw = solver.Value(charge_variables[slot]) / POWER_SCALE if battery else 0.0
            discharge_kw = solver.Value(discharge_variables[slot]) / POWER_SCALE if battery else 0.0
            soc_kwh = solver.Value(soc_variables[slot]) / SOC_SCALE if battery else 0.0
            ev_charge_kw = sum(
                solver.Value(ev_charge_variables[ev_load.id][slot]) / POWER_SCALE
                for ev_load in scenario.ev_loads
            )
            comfort_power_kw = sum(
                to_solver_int(comfort_load.power, comfort_load.power_unit) / POWER_SCALE
                for comfort_load in scenario.comfort_loads
                if slot in comfort_active_variables[comfort_load.id]
                and solver.Value(comfort_active_variables[comfort_load.id][slot])
            )
            battery_throughput_kwh += (charge_kw + discharge_kw) * resolution_hours
            energy_cost += import_kw * resolution_hours * context.tariffs[slot].price_per_kwh
            if context.export_tariffs is not None:
                export_revenue += (
                    export_kw * resolution_hours * context.export_tariffs[slot].price_per_kwh
                )
            export_kwh += export_kw * resolution_hours
            solar_kwh += solar_kw * resolution_hours
            slots.append(
                {
                    "slot": slot,
                    "load_power_kw": load_power / POWER_SCALE,
                    "solar_power_kw": solar_kw,
                    "grid_import_kw": import_kw,
                    "grid_export_kw": export_kw,
                    "battery_charge_kw": charge_kw,
                    "battery_discharge_kw": discharge_kw,
                    "battery_soc_kwh": soc_kwh,
                    "ev_charge_kw": ev_charge_kw,
                    "comfort_power_kw": comfort_power_kw,
                }
            )
        battery_degradation_cost = (
            battery_throughput_kwh * battery.degradation_cost_per_kwh
            if battery is not None and battery.degradation_cost_per_kwh is not None
            else 0.0
        )

        solver_evidence = SolverEvidence(
            solver_name="cp-sat",
            solver_version=_SOLVER_VERSION,
            num_search_workers=solver.parameters.num_search_workers,
            random_seed=solver.parameters.random_seed,
            wall_time_seconds=wall_time,
            tiers=solved_tiers,
            scenario_fingerprint=_scenario_fingerprint(scenario),
        )
        return build_result(
            scenario_id=scenario.id,
            status=(
                OptimizationStatus.NO_ACTION_REQUIRED
                if not plans and scenario.terminal_soc_policy is None
                else _status_for_tiers(status, degraded)
            ),
            plan=plans[0] if plans else None,
            plans=plans,
            objective_values={
                "start_slot_sum": float(sum(selected_slots.values())),
                "energy_cost": energy_cost - export_revenue + battery_degradation_cost,
                "export_revenue": export_revenue,
                "peak_import_kw": max((item["grid_import_kw"] for item in slots), default=0.0),
                "solar_self_consumption_kwh": max(0.0, solar_kwh - export_kwh),
                "conservative_mode_active": 1.0 if scenario.conservative else 0.0,
                "battery_throughput_kwh": battery_throughput_kwh,
                "battery_degradation_cost": battery_degradation_cost,
                "terminal_soc_kwh": (
                    solver.Value(soc_variables[-1]) / SOC_SCALE if battery is not None else 0.0
                ),
                "terminal_soc_value_eur": (
                    (
                        solver.Value(soc_variables[-1])
                        / SOC_SCALE
                        * scenario.terminal_soc_policy.value_eur_per_kwh
                    )
                    if battery is not None
                    and scenario.terminal_soc_policy is not None
                    and scenario.terminal_soc_policy.value_eur_per_kwh is not None
                    else 0.0
                ),
            },
            constraint_summary={
                "hard_satisfied": True,
                "battery_actuator_bound": battery is not None and battery.actuator is not None,
                "slots": slots,
                "violations": [],
                "soft_violations": _reported_soft_violations(solver, soft_violations),
                "forecast_confidence": forecast_confidence,
                "terminal_soc_policy": (
                    scenario.terminal_soc_policy.model_dump(mode="json")
                    if scenario.terminal_soc_policy is not None
                    else None
                ),
            },
            solver_evidence=solver_evidence,
        )


def _start_variables(model: Any, scenario: OptimizationScenario) -> dict[str, dict[int, Any]]:
    start_variables: dict[str, dict[int, Any]] = {}
    horizon_slots = scenario.horizon.slots
    for load in scenario.loads:
        latest = load.latest_slot
        if latest is None:
            latest = horizon_slots - load.duration_slots
        if load.deadline_slot is not None:
            latest = min(latest, load.deadline_slot - load.duration_slots + 1)
        variables = {
            slot: model.NewBoolVar(f"{load.id}_slot_{slot}")
            for slot in range(load.earliest_slot, latest + 1)
        }
        model.Add(sum(variables.values()) == 1)
        start_variables[load.id] = variables
    return start_variables


def _active_load_terms(
    scenario: OptimizationScenario,
    start_variables: dict[str, dict[int, Any]],
    slot: int,
    powers: dict[str, int] | None = None,
) -> list[Any]:
    terms: list[Any] = []
    for load in scenario.loads:
        power = (
            powers[load.id] if powers is not None else to_solver_int(load.power, load.power_unit)
        )
        for start, variable in start_variables[load.id].items():
            if start <= slot < start + load.duration_slots:
                terms.append(power * variable)
    return terms


def _add_energy_constraints(
    model: Any,
    scenario: OptimizationScenario,
    slot: int,
    active_load: Any,
    charge: Any,
    grid_import: Any,
    grid_export: Any,
    soc_variables: list[Any],
    grid_bound: int,
    soft_violations: list[tuple[str, int, Any]],
) -> None:
    # (actual quantity expression, limit-is-a-maximum, variable bound for a
    # possible violation IntVar) per supported constraint type.
    quantity_by_type: dict[str, tuple[Any, bool, int]] = {
        "max_house_power": (active_load + charge, True, grid_bound),
        "max_grid_import": (grid_import, True, grid_bound),
        "max_grid_export": (grid_export, True, grid_bound),
        "battery_min_soc": (
            soc_variables[slot] if soc_variables else None,
            False,
            SOC_SCALE * 10**6,
        ),
        "battery_max_soc": (
            soc_variables[slot] if soc_variables else None,
            True,
            SOC_SCALE * 10**6,
        ),
    }
    for constraint in scenario.constraints:
        resolved = quantity_by_type.get(constraint.type)
        if resolved is None:
            continue
        actual, is_max_bound, violation_bound = resolved
        if actual is None:
            continue
        limit = (
            to_energy_int(constraint.value)
            if constraint.type in {"battery_min_soc", "battery_max_soc"}
            else to_solver_int(constraint.value, constraint.unit)
        )
        if constraint.hard:
            if is_max_bound:
                model.Add(actual <= limit)
            else:
                model.Add(actual >= limit)
            continue
        violation = model.NewIntVar(0, violation_bound, f"soft_violation_{constraint.type}_{slot}")
        if is_max_bound:
            model.Add(violation >= actual - limit)
        else:
            model.Add(violation >= limit - actual)
        soft_violations.append((constraint.type, slot, violation))


def _objective_terms(
    objective: Objective,
    context: Any,
    resolution_hours: float,
    start_variables: dict[str, dict[int, Any]],
    grid_import: list[Any],
    grid_export: list[Any],
    peak_import: Any,
    charge_variables: list[Any] | None = None,
    discharge_variables: list[Any] | None = None,
) -> list[Any]:
    sign = -1 if objective.direction == "maximize" else 1
    weight = objective.weight
    terms: list[Any] = []
    if objective.name == "minimize_start":
        coefficient = sign * max(1, round(weight))
        for variables in start_variables.values():
            for start, variable in variables.items():
                terms.append(coefficient * start * variable)
    elif objective.name == "minimize_energy_cost":
        for slot, variable in enumerate(grid_import):
            coefficient = round(
                sign
                * weight
                * context.tariffs[slot].price_per_kwh
                * resolution_hours
                * OBJECTIVE_SCALE
            )
            terms.append(coefficient * variable)
        if context.export_tariffs is not None:
            for slot, variable in enumerate(grid_export):
                coefficient = round(
                    -sign
                    * weight
                    * context.export_tariffs[slot].price_per_kwh
                    * resolution_hours
                    * OBJECTIVE_SCALE
                )
                terms.append(coefficient * variable)
        battery = context.battery
        if (
            battery is not None
            and battery.degradation_cost_per_kwh is not None
            and battery.degradation_cost_per_kwh > 0
            and charge_variables is not None
            and discharge_variables is not None
        ):
            coefficient = round(
                sign
                * weight
                * battery.degradation_cost_per_kwh
                * resolution_hours
                * OBJECTIVE_SCALE
            )
            for charge_variable, discharge_variable in zip(
                charge_variables, discharge_variables, strict=True
            ):
                terms.append(coefficient * (charge_variable + discharge_variable))
    elif objective.name == "minimize_peak_import":
        terms.append(round(sign * weight * OBJECTIVE_SCALE) * peak_import)
    elif objective.name == "maximize_solar_self_consumption":
        # grid_export has inverse polarity to self-consumption (solar is a
        # fixed exogenous input, so maximizing self-consumption means
        # minimizing export), unlike every other objective's term variable —
        # apply the direction sign a second time to correct for that
        # inversion.
        for variable in grid_export:
            coefficient = round(-sign * weight * resolution_hours * OBJECTIVE_SCALE)
            terms.append(coefficient * variable)
    return terms


def _energy_objective_tiers(scenario: OptimizationScenario) -> list[list[Objective]]:
    objectives = scenario.objectives or [Objective(name="minimize_start", direction="minimize")]
    by_priority: dict[int, list[Objective]] = {}
    for objective in objectives:
        by_priority.setdefault(objective.priority, []).append(objective)
    return [by_priority[priority] for priority in sorted(by_priority)]


def _solve_tiers(
    model: Any,
    scenario: OptimizationScenario,
    start_variables: dict[str, dict[int, Any]],
    grid_import: list[Any],
    grid_export: list[Any],
    peak_import: Any,
    soft_violations: list[tuple[str, int, Any]],
    *,
    resolution_hours: float,
    context: Any,
    charge_variables: list[Any] | None = None,
    discharge_variables: list[Any] | None = None,
    terminal_soc_variable: Any | None = None,
) -> tuple[Any, int, list[SolvedTier], float, bool]:
    tiers: list[dict[str, Any]] = []
    if soft_violations:
        distinct_types = sorted({violation_type for violation_type, _slot, _var in soft_violations})
        tiers.append(
            {
                "priority": None,
                "terms": distinct_types,
                "tier_terms": [violation for (_type, _slot, violation) in soft_violations],
            }
        )
    for priority_group in _energy_objective_tiers(scenario):
        tier_terms: list[Any] = []
        for objective in priority_group:
            tier_terms.extend(
                _objective_terms(
                    objective,
                    context,
                    resolution_hours,
                    start_variables,
                    grid_import,
                    grid_export,
                    peak_import,
                    charge_variables,
                    discharge_variables,
                )
            )
        if tier_terms:
            tiers.append(
                {
                    "priority": priority_group[0].priority,
                    "terms": [objective.name for objective in priority_group],
                    "tier_terms": tier_terms,
                }
            )
    terminal_value = (
        scenario.terminal_soc_policy.value_eur_per_kwh
        if scenario.terminal_soc_policy is not None
        else None
    )
    if terminal_value is not None and terminal_value > 0 and terminal_soc_variable is not None:
        next_priority = (
            max(
                (
                    objective.priority
                    for priority_group in _energy_objective_tiers(scenario)
                    for objective in priority_group
                ),
                default=0,
            )
            + 1
        )
        tiers.append(
            {
                "priority": next_priority,
                "terms": ["terminal_soc_value"],
                "tier_terms": [-round(terminal_value * OBJECTIVE_SCALE) * terminal_soc_variable],
            }
        )

    solver: Any = None
    status: Any = cp_model.UNKNOWN
    solved_tiers: list[SolvedTier] = []
    wall_time = 0.0
    degraded = False
    start = time.monotonic()
    for tier in tiers:
        remaining = scenario.solver_time_limit_seconds - (time.monotonic() - start)
        if remaining <= 0:
            degraded = True
            break
        tier_terms = tier["tier_terms"]
        model.Minimize(sum(tier_terms))
        solver, status = _solve(model, scenario, time_limit=remaining)
        wall_time += solver.WallTime()
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return solver, status, solved_tiers, wall_time, degraded
        if status == cp_model.FEASIBLE:
            degraded = True
        achieved_value = solver.Value(sum(tier_terms))
        solved_tiers.append(
            SolvedTier(
                priority=tier["priority"], terms=tier["terms"], achieved_value=achieved_value
            )
        )
        model.Add(sum(tier_terms) <= achieved_value)
    if solver is None:
        remaining = scenario.solver_time_limit_seconds - (time.monotonic() - start)
        solver, status = _solve(model, scenario, time_limit=remaining)
        wall_time += solver.WallTime()
        if status == cp_model.FEASIBLE:
            degraded = True
    return solver, status, solved_tiers, wall_time, degraded


def _scenario_fingerprint(scenario: OptimizationScenario) -> str:
    canonical = json.dumps(scenario.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _reported_soft_violations(
    solver: Any, soft_violations: list[tuple[str, int, Any]]
) -> list[dict[str, Any]]:
    reported: list[dict[str, Any]] = []
    for constraint_type, slot, violation in soft_violations:
        value = solver.Value(violation)
        if value <= 0:
            continue
        is_soc_type = constraint_type in {"battery_min_soc", "battery_max_soc"}
        scale = SOC_SCALE if is_soc_type else POWER_SCALE
        reported.append({"type": constraint_type, "slot": slot, "amount": value / scale})
    return reported


def _load_power(load: Any, resolution_minutes: int) -> int:
    if load.power > 0:
        return to_solver_int(load.power, load.power_unit)
    if load.energy_required_kwh is None:
        return 0
    duration_hours = load.duration_slots * resolution_minutes / 60
    return to_solver_int(load.energy_required_kwh / duration_hours, "kW")


def _selected_slots(solver: Any, variables: dict[str, dict[int, Any]]) -> dict[str, int]:
    return {
        load_id: next(slot for slot, variable in slots.items() if solver.Value(variable))
        for load_id, slots in variables.items()
    }


def _proposal_plan(
    scenario: OptimizationScenario,
    selected_slots: dict[str, int],
    ev_charge_slots: dict[str, dict[int, float]] | None = None,
    comfort_active_slots: dict[str, list[int]] | None = None,
    battery_dispatch_slots: dict[int, tuple[float, float]] | None = None,
) -> list[Plan]:
    def _slot_execute_at(slot: int) -> datetime:
        return scenario.horizon.start + timedelta(
            minutes=slot * scenario.horizon.resolution_minutes
        )

    groups: dict[datetime, list[Any]] = {}
    for load in scenario.loads:
        slot = selected_slots[load.id]
        groups.setdefault(_slot_execute_at(slot), []).append(load)
        if load.end_command is not None:
            end_slot = slot + load.duration_slots
            if end_slot <= scenario.horizon.slots:
                groups.setdefault(_slot_execute_at(end_slot), []).append(
                    Command(
                        id=f"{scenario.id}:{load.id}:end",
                        device_id=load.device_id,
                        command=load.end_command,
                        value=load.end_value,
                        unit=load.unit,
                        idempotency_key=f"optimization:{scenario.id}:{load.id}:end",
                        intent=f"scheduled_slot:{end_slot}",
                    )
                )
    ev_loads_by_id = {ev_load.id: ev_load for ev_load in scenario.ev_loads}
    for ev_id, slot_charges in (ev_charge_slots or {}).items():
        ev_load = ev_loads_by_id[ev_id]
        positive_slots = [slot for slot, charge_kw in slot_charges.items() if charge_kw > 0]
        if not positive_slots:
            continue
        first_slot = min(positive_slots)
        last_slot = max(positive_slots)
        for slot in range(first_slot, last_slot + 1):
            charge_kw = slot_charges.get(slot, 0.0)
            groups.setdefault(_slot_execute_at(slot), []).append(
                Command(
                    id=f"{scenario.id}:{ev_load.id}:{slot}",
                    device_id=ev_load.device_id,
                    command=ev_load.command,
                    value=charge_kw,
                    unit="kW",
                    idempotency_key=f"optimization:{scenario.id}:{ev_load.id}:{slot}",
                    intent=f"scheduled_slot:{slot}",
                )
            )
        end_slot = last_slot + 1
        groups.setdefault(_slot_execute_at(end_slot), []).append(
            Command(
                id=f"{scenario.id}:{ev_load.id}:end",
                device_id=ev_load.device_id,
                command=ev_load.command,
                value=0.0,
                unit="kW",
                idempotency_key=f"optimization:{scenario.id}:{ev_load.id}:end",
                intent=f"scheduled_slot:{end_slot}",
            )
        )
    comfort_loads_by_id = {comfort_load.id: comfort_load for comfort_load in scenario.comfort_loads}
    for comfort_id, active_slots in (comfort_active_slots or {}).items():
        comfort_load = comfort_loads_by_id[comfort_id]
        sorted_slots = sorted(set(active_slots))
        if not sorted_slots:
            continue
        run_start = sorted_slots[0]
        previous_slot = sorted_slots[0]
        runs: list[tuple[int, int]] = []
        for slot in sorted_slots[1:]:
            if slot != previous_slot + 1:
                runs.append((run_start, previous_slot + 1))
                run_start = slot
            previous_slot = slot
        runs.append((run_start, previous_slot + 1))
        for run_start, run_end in runs:
            assert comfort_load.end_command is not None
            groups.setdefault(_slot_execute_at(run_start), []).append(
                Command(
                    id=f"{scenario.id}:{comfort_load.id}:start:{run_start}",
                    device_id=comfort_load.device_id,
                    command=comfort_load.command,
                    value=comfort_load.value,
                    unit=comfort_load.unit,
                    idempotency_key=(
                        f"optimization:{scenario.id}:{comfort_load.id}:start:{run_start}"
                    ),
                    intent=f"scheduled_slot:{run_start}",
                )
            )
            groups.setdefault(_slot_execute_at(run_end), []).append(
                Command(
                    id=f"{scenario.id}:{comfort_load.id}:end:{run_end}",
                    device_id=comfort_load.device_id,
                    command=comfort_load.end_command,
                    value=comfort_load.end_value,
                    unit=comfort_load.unit,
                    idempotency_key=(f"optimization:{scenario.id}:{comfort_load.id}:end:{run_end}"),
                    intent=f"scheduled_slot:{run_end}",
                )
            )
    battery = scenario.energy_context.battery if scenario.energy_context is not None else None
    actuator = battery.actuator if battery is not None else None
    if actuator is not None:
        # The physical inverter state is not known to the optimizer.  A
        # synthetic STOPPED predecessor must therefore never suppress slot 0:
        # takeover and readback own the real baseline.
        previous_state: tuple[str, float | None] | None = None
        for slot in range(scenario.horizon.slots):
            charge_kw, discharge_kw = (battery_dispatch_slots or {}).get(slot, (0.0, 0.0))
            if charge_kw > 0 and discharge_kw > 0:
                raise ValueError("battery dispatch cannot charge and discharge simultaneously")
            if charge_kw > 0:
                state: tuple[str, float | None] = (actuator.charge_command, charge_kw)
                unit = actuator.power_unit
            elif discharge_kw > 0:
                state = (actuator.discharge_command, discharge_kw)
                unit = actuator.power_unit
            else:
                state = (actuator.stop_command, None)
                unit = None
            if previous_state is not None and state == previous_state:
                continue
            if state[1] is None:
                feedback_value = 0.0
            elif state[0] == actuator.charge_command:
                feedback_value = state[1] * (
                    1 if actuator.power_feedback_convention == "charge_positive" else -1
                )
            else:
                feedback_value = state[1] * (
                    -1 if actuator.power_feedback_convention == "charge_positive" else 1
                )
            groups.setdefault(_slot_execute_at(slot), []).append(
                Command(
                    id=f"{scenario.id}:battery:{slot}",
                    device_id=actuator.device_id,
                    command=state[0],
                    value=state[1],
                    unit=unit,
                    idempotency_key=f"optimization:{scenario.id}:battery:{slot}",
                    intent=(
                        f"takeover_first_slot:{slot}"
                        if previous_state is None
                        else f"scheduled_slot:{slot}"
                    ),
                    postconditions=[
                        CommandPostcondition(
                            capability=actuator.power_feedback_capability,
                            expected=feedback_value,
                            tolerance=actuator.power_feedback_tolerance_kw,
                            settle_timeout_seconds=(actuator.power_feedback_settle_timeout_seconds),
                            poll_interval_seconds=actuator.power_feedback_poll_interval_seconds,
                            reconcile_capabilities=(
                                [actuator.soc_reconciliation_capability]
                                if actuator.soc_reconciliation_capability is not None
                                else []
                            ),
                        )
                    ],
                )
            )
            previous_state = state
        if previous_state is not None and previous_state[0] != actuator.stop_command:
            horizon_slot = scenario.horizon.slots
            groups.setdefault(_slot_execute_at(horizon_slot), []).append(
                Command(
                    id=f"{scenario.id}:battery:{horizon_slot}:end",
                    device_id=actuator.device_id,
                    command=actuator.stop_command,
                    idempotency_key=f"optimization:{scenario.id}:battery:{horizon_slot}:end",
                    intent=f"scheduled_slot:{horizon_slot}",
                    postconditions=[
                        CommandPostcondition(
                            capability=actuator.power_feedback_capability,
                            expected=0.0,
                            tolerance=actuator.power_feedback_tolerance_kw,
                            settle_timeout_seconds=(actuator.power_feedback_settle_timeout_seconds),
                            poll_interval_seconds=actuator.power_feedback_poll_interval_seconds,
                            reconcile_capabilities=(
                                [actuator.soc_reconciliation_capability]
                                if actuator.soc_reconciliation_capability is not None
                                else []
                            ),
                        )
                    ],
                )
            )
    ordered_times = sorted(groups)
    single = len(ordered_times) == 1
    plans: list[Plan] = []
    for index, execute_at in enumerate(ordered_times):
        plan_id = f"proposal-{scenario.id}" if single else f"proposal-{scenario.id}-{index}"
        plans.append(
            Plan(
                id=plan_id,
                execute_at=execute_at,
                commands=[
                    item
                    if isinstance(item, Command)
                    else Command(
                        id=f"{scenario.id}:{item.id}",
                        device_id=item.device_id,
                        command=item.command,
                        value=item.value,
                        unit=item.unit,
                        idempotency_key=f"optimization:{scenario.id}:{item.id}",
                        intent=f"scheduled_slot:{selected_slots[item.id]}",
                    )
                    for item in groups[execute_at]
                ],
            )
        )
    return plans


def _solve(model: Any, scenario: OptimizationScenario, *, time_limit: float) -> tuple[Any, int]:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(time_limit, 0.0)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    return solver, solver.Solve(model)


def _failure_result(scenario: OptimizationScenario, status: int) -> OptimizationResult | None:
    if status == cp_model.INFEASIBLE:
        return build_result(
            scenario_id=scenario.id,
            status=OptimizationStatus.INFEASIBLE,
            diagnostics=[
                {
                    "code": "infeasible",
                    "message": (
                        "Hard power constraints cannot accommodate the requested loads"
                        if scenario.energy_context is None
                        else "Declared hard energy constraints cannot be satisfied"
                    ),
                }
            ],
            constraint_summary={"hard_satisfied": False, "soft_violations": []},
        )
    if status == cp_model.UNKNOWN:
        return build_result(
            scenario_id=scenario.id,
            status=OptimizationStatus.TIMEOUT,
            diagnostics=[{"code": "timeout", "message": "Solver did not finish in time"}],
        )
    if status not in {cp_model.FEASIBLE, cp_model.OPTIMAL}:
        return build_result(
            scenario_id=scenario.id,
            status=OptimizationStatus.UNKNOWN,
            diagnostics=[
                {"code": "solver_unknown", "message": "Solver returned an unknown status"}
            ],
        )
    return None


def _status(status: int) -> OptimizationStatus:
    return OptimizationStatus.OPTIMAL if status == cp_model.OPTIMAL else OptimizationStatus.FEASIBLE


def _status_for_tiers(status: int, degraded: bool) -> OptimizationStatus:
    if status == cp_model.OPTIMAL and not degraded:
        return OptimizationStatus.OPTIMAL_HIERARCHY
    return OptimizationStatus.FEASIBLE_HIERARCHY


def to_solver_int(value: float, unit: str) -> int:
    if unit == "W":
        return round(value * 1000)
    if unit == "kW":
        return round(value * POWER_SCALE)
    raise ValueError(f"Unsupported solver unit: {unit}")


def to_energy_int(value: float) -> int:
    return round(value * SOC_SCALE)


def _objective_weight(objectives: list[Objective], name: str) -> float:
    for objective in objectives:
        if objective.name == name:
            return objective.weight if objective.direction == "minimize" else -objective.weight
    return 1.0 if name == "minimize_start" and not objectives else 0.0
