import pytest

from domoai.domain.models import AdapterSnapshot
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import ExteriorTemperaturePoint, HVACActuator, ThermalProfile
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.energy import energy_context_for, energy_horizon


def _thermal_profile(**overrides: object) -> ThermalProfile:
    payload: dict[str, object] = {
        "capacitance_kwh_per_c": 5.0,
        "ua_kw_per_c": 0.2,
        "initial_temperature_c": 20.0,
        "comfort_min_c": -50.0,
        "comfort_max_c": 50.0,
        "max_heat_kw": 3.0,
        "max_cool_kw": 3.0,
        "heating_cop": 3.0,
        "cooling_cop": 2.5,
    }
    payload.update(overrides)
    return ThermalProfile(**payload)


def test_thermal_profile_alone_solves_and_drifts_passively_toward_exterior() -> None:
    horizon = energy_horizon(slots=4, resolution_minutes=15)
    base_context = energy_context_for(horizon, with_battery=False)
    context = base_context.model_copy(
        update={
            # Zeroed so surplus solar can't make heating a zero-cost tie
            # (the shared fixture's default has nonzero solar at some
            # slots, which would make "hvac stays at 0" an unreliable
            # solver tie-break rather than a deterministic cost result).
            "solar_forecast": [
                point.model_copy(update={"power": 0.0}) for point in base_context.solar_forecast
            ],
            "thermal": _thermal_profile(initial_temperature_c=20.0),
            "exterior_temperature_forecast": [
                ExteriorTemperaturePoint(slot=slot, temperature_c=10.0) for slot in range(4)
            ],
        }
    )
    scenario = OptimizationScenario(
        id="thermal-passive-1",
        horizon=horizon,
        energy_context=context,
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    assert result.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.NO_ACTION_REQUIRED,
    }
    temperatures = [slot["indoor_temperature_c"] for slot in result.constraint_summary["slots"]]
    # slot 0's reported value is the start-of-slot-0 state, i.e. exactly
    # initial_temperature_c by construction -- the drift shows up from
    # slot 1 onward. Colder outside than inside, no incentive to heat
    # (pure cost minimization, no comfort constraint) -- temperature must
    # drift downward monotonically toward the exterior temperature, never up.
    assert temperatures[0] == pytest.approx(20.0, abs=0.01)
    assert temperatures[-1] < temperatures[0]
    for earlier, later in zip(temperatures, temperatures[1:], strict=False):
        assert later <= earlier + 1e-6
    hvac_powers = [slot["hvac_power_kw"] for slot in result.constraint_summary["slots"]]
    assert all(value == pytest.approx(0.0, abs=1e-6) for value in hvac_powers)


def test_thermal_profile_without_forecast_holds_initial_temperature() -> None:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    base_context = energy_context_for(horizon, with_battery=False)
    context = base_context.model_copy(
        update={
            # Zeroed for the same reason as the passive-drift test above:
            # surplus solar with no export incentive makes heating a
            # zero-cost tie the solver is free to take, unrelated to the
            # thing this test actually checks.
            "solar_forecast": [
                point.model_copy(update={"power": 0.0}) for point in base_context.solar_forecast
            ],
            "thermal": _thermal_profile(initial_temperature_c=20.0),
        }
    )
    scenario = OptimizationScenario(
        id="thermal-no-forecast-1",
        horizon=horizon,
        energy_context=context,
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    temperatures = [slot["indoor_temperature_c"] for slot in result.constraint_summary["slots"]]
    # No exterior_temperature_forecast supplied -- falls back to the
    # initial temperature every slot (no UA loss drives any drift).
    assert all(value == pytest.approx(20.0, abs=0.01) for value in temperatures)


def test_hard_comfort_min_forces_heating_into_the_cheap_slot() -> None:
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    base_context = energy_context_for(horizon, with_battery=False)
    prices = [0.05, 0.50]
    context = base_context.model_copy(
        update={
            "tariffs": [
                point.model_copy(update={"price_per_kwh": price})
                for point, price in zip(base_context.tariffs, prices, strict=True)
            ],
            "solar_forecast": [
                point.model_copy(update={"power": 0.0}) for point in base_context.solar_forecast
            ],
            "thermal": _thermal_profile(
                initial_temperature_c=19.0, comfort_min_c=19.0, comfort_max_c=50.0
            ),
            "exterior_temperature_forecast": [
                ExteriorTemperaturePoint(slot=slot, temperature_c=5.0) for slot in range(2)
            ],
        }
    )
    scenario = OptimizationScenario(
        id="thermal-preheat-1",
        horizon=horizon,
        energy_context=context,
        constraints=[
            Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=True),
        ],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    # No actuator is bound (command emission is T012/T013's job, not the
    # recurrence's) -- NO_ACTION_REQUIRED is expected here since no Plan is
    # ever produced without a bound actuator, independent of whether the
    # internal decision variables genuinely reflect the intended physics
    # (which is what constraint_summary["slots"] below actually checks).
    assert result.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.NO_ACTION_REQUIRED,
    }
    slots = result.constraint_summary["slots"]
    temperatures = [slot["indoor_temperature_c"] for slot in slots]
    assert all(value >= 19.0 - 0.01 for value in temperatures)
    hvac_powers = [slot["hvac_power_kw"] for slot in slots]
    # The cheap slot (0) must carry at least as much heating as the
    # expensive slot (1) -- cost minimization prefers front-loading.
    assert hvac_powers[0] >= hvac_powers[1] - 1e-6
    assert hvac_powers[0] > 0.0


def test_soft_comfort_min_reports_violation_instead_of_infeasible() -> None:
    horizon = energy_horizon(slots=1, resolution_minutes=15)
    base_context = energy_context_for(horizon, with_battery=False)
    context = base_context.model_copy(
        update={
            "solar_forecast": [
                point.model_copy(update={"power": 0.0}) for point in base_context.solar_forecast
            ],
            "thermal": _thermal_profile(
                initial_temperature_c=15.0,
                comfort_min_c=19.0,
                comfort_max_c=50.0,
                # Deliberately too little heating power to reach comfort
                # range in one slot from a cold start -- forces a genuine
                # violation rather than a satisfiable-but-untested one.
                max_heat_kw=0.001,
                max_cool_kw=0.0,
            ),
            "exterior_temperature_forecast": [ExteriorTemperaturePoint(slot=0, temperature_c=5.0)],
        }
    )
    soft_scenario = OptimizationScenario(
        id="thermal-soft-violation-1",
        horizon=horizon,
        energy_context=context,
        constraints=[Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=False)],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    soft_result = CpSatOptimizer(DeviceRegistry()).optimize(soft_scenario)

    assert soft_result.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.NO_ACTION_REQUIRED,
    }
    violations = soft_result.constraint_summary["soft_violations"]
    assert any(
        violation["type"] == "comfort_temp_min" and violation["amount"] > 0
        for violation in violations
    )

    hard_scenario = soft_scenario.model_copy(
        update={
            "constraints": [
                Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=True)
            ]
        }
    )

    hard_result = CpSatOptimizer(DeviceRegistry()).optimize(hard_scenario)

    assert hard_result.status is OptimizationStatus.INFEASIBLE


def _registry_with_thermostat() -> DeviceRegistry:
    registry = DeviceRegistry()
    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "climate.thermostat",
                    "device_id": "thermostat-device",
                    "domain": "climate",
                    "semantic_type": "energy",
                    "name": "Home thermostat",
                    "canonical_id": "thermostat.home",
                    "capabilities": [
                        {
                            "name": "hvac_power",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": True,
                            "minimum": 0,
                            "maximum": 3,
                            "commands": ["heat_thermostat", "cool_thermostat", "stop_thermostat"],
                        }
                    ],
                    "identity_keys": ["fixture:device:thermostat-device"],
                    "connections": ["fixture:thermostat-device"],
                }
            ]
        ),
        "fixture",
    )
    return registry


def test_bound_hvac_actuator_emits_deduplicated_postcondition_verified_commands() -> None:
    registry = _registry_with_thermostat()
    horizon = energy_horizon(slots=2, resolution_minutes=15)
    base_context = energy_context_for(horizon, with_battery=False)
    prices = [0.05, 0.50]
    context = base_context.model_copy(
        update={
            "tariffs": [
                point.model_copy(update={"price_per_kwh": price})
                for point, price in zip(base_context.tariffs, prices, strict=True)
            ],
            "solar_forecast": [
                point.model_copy(update={"power": 0.0}) for point in base_context.solar_forecast
            ],
            "thermal": _thermal_profile(
                initial_temperature_c=19.0, comfort_min_c=19.0, comfort_max_c=50.0
            ).model_copy(
                update={
                    "actuator": HVACActuator(
                        device_id="thermostat.home",
                        capability="hvac_power",
                        heat_command="heat_thermostat",
                        cool_command="cool_thermostat",
                        stop_command="stop_thermostat",
                        power_feedback_capability="hvac_power",
                        power_feedback_tolerance_kw=0.1,
                    )
                }
            ),
            "exterior_temperature_forecast": [
                ExteriorTemperaturePoint(slot=slot, temperature_c=5.0) for slot in range(2)
            ],
        }
    )
    scenario = OptimizationScenario(
        id="thermal-dispatch-1",
        horizon=horizon,
        energy_context=context,
        constraints=[Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=True)],
        objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
    )

    result = CpSatOptimizer(registry).optimize(scenario)

    assert result.status in {
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL_HIERARCHY,
    }
    assert result.plan is not None
    commands = [command for plan in result.plans for command in plan.commands]
    heat_commands = [command for command in commands if command.command == "heat_thermostat"]
    assert heat_commands
    for command in heat_commands:
        assert command.device_id == "thermostat.home"
        assert command.postconditions
        postcondition = command.postconditions[0]
        assert postcondition.capability == "hvac_power"
        assert postcondition.expected == pytest.approx(command.value, abs=1e-9)
    # State-transition deduplication: consecutive commands never repeat the
    # same (command, value) state back to back -- mirrors the existing
    # battery dispatch dedup guarantee (one Command per transition, not one
    # per slot).
    thermostat_commands = [c for c in commands if c.device_id == "thermostat.home"]
    states = [(c.command, c.value) for c in thermostat_commands]
    for previous, current in zip(states, states[1:], strict=False):
        assert current != previous
