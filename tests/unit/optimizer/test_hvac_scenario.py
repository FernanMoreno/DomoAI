from domoai.domain.models import AdapterSnapshot
from domoai.optimizer.energy import HVACActuator, ThermalProfile
from domoai.optimizer.scenario import (
    Constraint,
    OptimizationScenario,
    validate_scenario,
)
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.energy import energy_context_for


def _thermal_profile() -> ThermalProfile:
    return ThermalProfile(
        capacitance_kwh_per_c=5.0,
        ua_kw_per_c=0.2,
        initial_temperature_c=20.0,
        comfort_min_c=19.0,
        comfort_max_c=22.0,
        max_heat_kw=3.0,
        max_cool_kw=2.0,
        heating_cop=3.0,
        cooling_cop=2.5,
    )


def test_comfort_temp_constraints_require_thermal_profile() -> None:
    context = energy_context_for()
    assert context.thermal is None
    scenario = OptimizationScenario(
        id="comfort-no-thermal-1",
        horizon=context.horizon,
        energy_context=context,
        constraints=[
            Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=False),
        ],
    )

    diagnostics = validate_scenario(scenario, DeviceRegistry())

    assert any(item.code == "missing_thermal_profile" for item in diagnostics)


def test_comfort_temp_constraints_accepted_with_thermal_profile() -> None:
    context = energy_context_for().model_copy(update={"thermal": _thermal_profile()})
    scenario = OptimizationScenario(
        id="comfort-with-thermal-1",
        horizon=context.horizon,
        energy_context=context,
        constraints=[
            Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=False),
            Constraint(type="comfort_temp_max", value=22.0, unit="degC", hard=False),
        ],
    )

    diagnostics = validate_scenario(scenario, DeviceRegistry())

    assert not any(item.code == "unsupported_constraint" for item in diagnostics)
    assert not any(item.code == "missing_thermal_profile" for item in diagnostics)


def test_comfort_temp_constraints_reject_wrong_unit() -> None:
    context = energy_context_for().model_copy(update={"thermal": _thermal_profile()})
    scenario = OptimizationScenario(
        id="comfort-bad-unit-1",
        horizon=context.horizon,
        energy_context=context,
        constraints=[
            Constraint(type="comfort_temp_min", value=19.0, unit="kW", hard=False),
        ],
    )

    diagnostics = validate_scenario(scenario, DeviceRegistry())

    assert any(item.code == "invalid_unit" for item in diagnostics)


def test_hvac_actuator_requires_current_canonical_route() -> None:
    registry = DeviceRegistry()
    context = energy_context_for().model_copy(
        update={
            "thermal": _thermal_profile().model_copy(
                update={
                    "actuator": HVACActuator(
                        device_id="missing.thermostat",
                        capability="hvac_power",
                        heat_command="heat",
                        cool_command="cool",
                        stop_command="stop",
                        power_feedback_capability="hvac_power",
                        power_feedback_tolerance_kw=0.1,
                    )
                }
            )
        }
    )
    scenario = OptimizationScenario(
        id="hvac-route-required-1",
        horizon=context.horizon,
        energy_context=context,
    )

    diagnostics = validate_scenario(scenario, registry)

    assert any(item.code == "missing_device" for item in diagnostics)


def test_hvac_actuator_requires_readable_power_feedback_capability() -> None:
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
                            "name": "hvac_control",
                            "kind": "number",
                            "unit": "kW",
                            "readable": False,
                            "writable": True,
                            "commands": ["heat", "cool", "stop"],
                        }
                    ],
                    "identity_keys": ["fixture:device:thermostat-device"],
                    "connections": ["fixture:thermostat-device"],
                }
            ]
        ),
        "fixture",
    )
    context = energy_context_for().model_copy(
        update={
            "thermal": _thermal_profile().model_copy(
                update={
                    "actuator": HVACActuator(
                        device_id="thermostat.home",
                        capability="hvac_control",
                        heat_command="heat",
                        cool_command="cool",
                        stop_command="stop",
                        power_feedback_capability="hvac_control",
                        power_feedback_tolerance_kw=0.1,
                    )
                }
            )
        }
    )
    scenario = OptimizationScenario(
        id="hvac-feedback-required-1",
        horizon=context.horizon,
        energy_context=context,
    )

    diagnostics = validate_scenario(scenario, registry)

    assert any(item.code == "missing_feedback_capability" for item in diagnostics)
