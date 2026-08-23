from datetime import UTC, datetime

from domoai.optimizer.cp_sat import _proposal_plan
from domoai.optimizer.energy import BatteryActuator
from domoai.optimizer.scenario import OptimizationScenario
from tests.fixtures.energy import energy_context_for, energy_horizon


def _scenario_with_battery(
    *, slots: int = 4, soc_reconciliation_capability: str | None = None
) -> OptimizationScenario:
    horizon = energy_horizon(slots=slots)
    context = energy_context_for(horizon=horizon)
    assert context.battery is not None
    context = context.model_copy(
        update={
            "battery": context.battery.model_copy(
                update={
                    "actuator": BatteryActuator(
                        device_id="garage.home_battery",
                        capability="battery_power",
                        charge_command="charge_battery",
                        discharge_command="discharge_battery",
                        stop_command="stop_battery",
                        power_feedback_capability="battery_power",
                        power_feedback_tolerance_kw=0.1,
                        power_feedback_settle_timeout_seconds=5.0,
                        power_feedback_poll_interval_seconds=0.25,
                        soc_reconciliation_capability=soc_reconciliation_capability,
                    )
                }
            )
        }
    )
    return OptimizationScenario(
        id="dispatchable-battery-1",
        horizon=horizon,
        energy_context=context,
    )


def test_battery_compiler_emits_direction_transitions_and_stop() -> None:
    scenario = _scenario_with_battery()

    plans = _proposal_plan(
        scenario,
        selected_slots={},
        battery_dispatch_slots={
            0: (2.0, 0.0),
            1: (2.0, 0.0),
            2: (0.0, 1.5),
            3: (0.0, 0.0),
        },
    )

    assert len(plans) == 3
    commands = [command for plan in plans for command in plan.commands]
    assert [command.command for command in commands] == [
        "charge_battery",
        "discharge_battery",
        "stop_battery",
    ]
    assert [command.value for command in commands] == [2.0, 1.5, None]
    assert [
        (
            item.capability,
            item.expected,
            item.tolerance,
            item.settle_timeout_seconds,
            item.poll_interval_seconds,
        )
        for command in commands
        for item in command.postconditions
    ] == [
        ("battery_power", 2.0, 0.1, 5.0, 0.25),
        ("battery_power", -1.5, 0.1, 5.0, 0.25),
        ("battery_power", 0.0, 0.1, 5.0, 0.25),
    ]
    assert [command.device_id for command in commands] == [
        "garage.home_battery",
        "garage.home_battery",
        "garage.home_battery",
    ]


def test_battery_compiler_stops_exactly_at_horizon_end() -> None:
    scenario = _scenario_with_battery(slots=2)

    plans = _proposal_plan(
        scenario,
        selected_slots={},
        battery_dispatch_slots={1: (0.0, 2.0)},
    )

    commands = [command for plan in plans for command in plan.commands]
    assert [command.command for command in commands] == [
        "discharge_battery",
        "stop_battery",
    ]
    assert commands[-1].value is None
    assert plans[-1].execute_at == scenario.horizon.end


def test_battery_compiler_applies_discharge_positive_feedback_convention() -> None:
    scenario = _scenario_with_battery()
    assert scenario.energy_context is not None
    assert scenario.energy_context.battery is not None
    actuator = scenario.energy_context.battery.actuator
    assert actuator is not None
    actuator = actuator.model_copy(update={"power_feedback_convention": "discharge_positive"})
    scenario = scenario.model_copy(
        update={
            "energy_context": scenario.energy_context.model_copy(
                update={
                    "battery": scenario.energy_context.battery.model_copy(
                        update={"actuator": actuator}
                    )
                }
            )
        }
    )

    plans = _proposal_plan(
        scenario,
        selected_slots={},
        battery_dispatch_slots={0: (2.0, 0.0), 1: (0.0, 1.5)},
    )

    commands = [command for plan in plans for command in plan.commands]
    assert [command.postconditions[0].expected for command in commands] == [-2.0, 1.5, 0.0]


def test_battery_compiler_carries_explicit_soc_reconciliation_to_each_transition() -> None:
    scenario = _scenario_with_battery(soc_reconciliation_capability="battery.soc")

    plans = _proposal_plan(
        scenario,
        selected_slots={},
        battery_dispatch_slots={0: (2.0, 0.0), 1: (0.0, 0.0)},
    )

    commands = [command for plan in plans for command in plan.commands]

    assert [command.command for command in commands] == ["charge_battery", "stop_battery"]
    assert [
        command.postconditions[0].reconcile_capabilities for command in commands
    ] == [["battery.soc"], ["battery.soc"]]


def test_unbound_energy_context_remains_valid_for_analysis() -> None:
    scenario = OptimizationScenario(
        id="unbound-battery-analysis-1",
        horizon=energy_horizon(),
        energy_context=energy_context_for(),
    )

    assert scenario.energy_context is not None
    assert scenario.energy_context.battery is not None
    assert scenario.energy_context.battery.actuator is None
    assert scenario.horizon.start == datetime(2026, 8, 15, tzinfo=UTC)
