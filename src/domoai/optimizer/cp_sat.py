"""Integer-scaled CP-SAT translation for proposal-only scheduling."""

from __future__ import annotations

from typing import Any

from ortools.sat.python import cp_model

from domoai.domain.models import Command, Plan
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus, build_result
from domoai.optimizer.scenario import Objective, OptimizationScenario, validate_scenario
from domoai.runtime.registry import DeviceRegistry

POWER_SCALE = 1_000_000  # mW when the public unit is kW
SOC_SCALE = 1_000_000  # mWh when the public unit is kWh
EFFICIENCY_SCALE = 1_000
OBJECTIVE_SCALE = 1_000_000


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

        solver, status = _solve(model, scenario)
        failure = _failure_result(scenario, status)
        if failure is not None:
            return failure

        selected_slots = _selected_slots(solver, start_variables)
        return build_result(
            scenario_id=scenario.id,
            status=_status(status),
            plan=_proposal_plan(scenario, selected_slots),
            objective_values={"start_slot_sum": float(sum(selected_slots.values()))},
            constraint_summary={"hard_satisfied": True, "soft_violations": []},
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
        solar_powers = [to_solver_int(point.power, point.unit) for point in context.solar_forecast]

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

        load_bound = sum(load_powers.values())
        solar_bound = max(solar_powers, default=0)
        grid_bound = max(1, load_bound + solar_bound + max_charge + max_discharge) * 2
        for constraint in scenario.constraints:
            if constraint.type in {"max_grid_import", "max_grid_export"}:
                grid_bound = max(grid_bound, to_solver_int(constraint.value, constraint.unit))

        grid_import: list[Any] = []
        grid_export: list[Any] = []
        peak_import = model.NewIntVar(0, grid_bound, "peak_grid_import")
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

            active_load = sum(_active_load_terms(scenario, start_variables, slot, load_powers))
            model.Add(
                active_load + charge
                == solar_powers[slot] + grid_in + discharge - grid_out
            )
            _add_energy_constraints(
                model,
                scenario,
                slot,
                active_load,
                charge,
                grid_in,
                grid_out,
                soc_variables,
            )

        _add_energy_objective(
            model,
            scenario,
            start_variables,
            grid_import,
            grid_export,
            peak_import,
        )

        solver, status = _solve(model, scenario)
        failure = _failure_result(scenario, status)
        if failure is not None:
            return failure

        selected_slots = _selected_slots(solver, start_variables)
        slots: list[dict[str, float | int]] = []
        energy_cost = 0.0
        export_kwh = 0.0
        solar_kwh = 0.0
        resolution_hours = scenario.horizon.resolution_minutes / 60
        for slot in range(horizon_slots):
            load_power = sum(
                load_powers[load.id]
                for load in scenario.loads
                if any(
                    start <= slot < start + load.duration_slots
                    and solver.Value(variable)
                    for start, variable in start_variables[load.id].items()
                )
            )
            solar_kw = solar_powers[slot] / POWER_SCALE
            import_kw = solver.Value(grid_import[slot]) / POWER_SCALE
            export_kw = solver.Value(grid_export[slot]) / POWER_SCALE
            charge_kw = solver.Value(charge_variables[slot]) / POWER_SCALE if battery else 0.0
            discharge_kw = (
                solver.Value(discharge_variables[slot]) / POWER_SCALE if battery else 0.0
            )
            soc_kwh = solver.Value(soc_variables[slot]) / SOC_SCALE if battery else 0.0
            energy_cost += import_kw * resolution_hours * context.tariffs[slot].price_per_kwh
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
                }
            )

        return build_result(
            scenario_id=scenario.id,
            status=_status(status),
            plan=_proposal_plan(scenario, selected_slots),
            objective_values={
                "start_slot_sum": float(sum(selected_slots.values())),
                "energy_cost": energy_cost,
                "peak_import_kw": max((item["grid_import_kw"] for item in slots), default=0.0),
                "solar_self_consumption_kwh": max(0.0, solar_kwh - export_kwh),
            },
            constraint_summary={
                "hard_satisfied": True,
                "slots": slots,
                "violations": [],
                "soft_violations": [],
            },
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
            powers[load.id]
            if powers is not None
            else to_solver_int(load.power, load.power_unit)
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
) -> None:
    for constraint in scenario.constraints:
        if not constraint.hard:
            continue
        if constraint.type == "max_house_power":
            model.Add(active_load + charge <= to_solver_int(constraint.value, constraint.unit))
        elif constraint.type == "max_grid_import":
            model.Add(grid_import <= to_solver_int(constraint.value, constraint.unit))
        elif constraint.type == "max_grid_export":
            model.Add(grid_export <= to_solver_int(constraint.value, constraint.unit))
        elif constraint.type == "battery_min_soc":
            model.Add(soc_variables[slot] >= to_energy_int(constraint.value))
        elif constraint.type == "battery_max_soc":
            model.Add(soc_variables[slot] <= to_energy_int(constraint.value))


def _add_energy_objective(
    model: Any,
    scenario: OptimizationScenario,
    start_variables: dict[str, dict[int, Any]],
    grid_import: list[Any],
    grid_export: list[Any],
    peak_import: Any,
) -> None:
    context = scenario.energy_context
    assert context is not None
    resolution_hours = scenario.horizon.resolution_minutes / 60
    objectives = scenario.objectives or [
        Objective(name="minimize_start", direction="minimize")
    ]
    terms: list[Any] = []
    for objective in objectives:
        sign = -1 if objective.direction == "maximize" else 1
        weight = objective.weight
        if objective.name == "minimize_start":
            coefficient = max(1, round(sign * weight))
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
        elif objective.name == "minimize_peak_import":
            terms.append(round(sign * weight * OBJECTIVE_SCALE) * peak_import)
        elif objective.name == "maximize_solar_self_consumption":
            for variable in grid_export:
                coefficient = round(sign * weight * resolution_hours * OBJECTIVE_SCALE)
                terms.append(coefficient * variable)
    if terms:
        model.Minimize(sum(terms))


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


def _proposal_plan(scenario: OptimizationScenario, selected_slots: dict[str, int]) -> Plan:
    return Plan(
        id=f"proposal-{scenario.id}",
        commands=[
            Command(
                id=f"{scenario.id}:{load.id}",
                device_id=load.device_id,
                command=load.command,
                value=load.value,
                unit=load.unit,
                idempotency_key=f"optimization:{scenario.id}:{load.id}",
                intent=f"scheduled_slot:{selected_slots[load.id]}",
            )
            for load in scenario.loads
        ],
    )


def _solve(model: Any, scenario: OptimizationScenario) -> tuple[Any, int]:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = scenario.solver_time_limit_seconds
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
    return (
        OptimizationStatus.OPTIMAL
        if status == cp_model.OPTIMAL
        else OptimizationStatus.FEASIBLE
    )


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
