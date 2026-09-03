from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import (
    AdapterExecutionAck,
    AdapterSnapshot,
    Command,
    CommandPostcondition,
    Plan,
    RiskClass,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulator
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import BaseLoadPoint, BatteryActuator
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import (
    Constraint,
    Objective,
    OptimizationScenario,
    TerminalSOCPolicy,
    validate_scenario,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.risk_classifier import RiskClassifier, RiskOverride
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, energy_horizon


class _BatterySimulationAdapter(SimulatedHomeAdapter):
    """AdapterPort projection for a deterministic physical battery model."""

    adapter_id = "battery_simulator"

    def __init__(self, simulator: BatterySimulator, *, clock: FixedClock) -> None:
        super().__init__(clock=clock)
        self.simulator = simulator
        self.clock = clock
        self._battery_snapshot = AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "battery.power",
                    "device_id": "battery-device",
                    "canonical_id": "battery.home",
                    "identity_keys": ["lab:battery-device"],
                    "connections": ["lab:battery-device"],
                    "name": "Battery power",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.power",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": False,
                        }
                    ],
                    "available": True,
                },
                {
                    "entity_id": "battery.soc",
                    "device_id": "battery-device",
                    "canonical_id": "battery.home",
                    "identity_keys": ["lab:battery-device"],
                    "connections": ["lab:battery-device"],
                    "name": "Battery SOC",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.soc",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        }
                    ],
                    "available": True,
                },
                {
                    "entity_id": "battery.capacity",
                    "device_id": "battery-device",
                    "canonical_id": "battery.home",
                    "identity_keys": ["lab:battery-device"],
                    "connections": ["lab:battery-device"],
                    "name": "Battery capacity",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.capacity",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        }
                    ],
                    "available": True,
                },
                {
                    "entity_id": "battery.control",
                    "device_id": "battery-device",
                    "canonical_id": "battery.home",
                    "identity_keys": ["lab:battery-device"],
                    "connections": ["lab:battery-device"],
                    "name": "Battery control",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.control",
                            "kind": "number",
                            "unit": "kW",
                            "readable": False,
                            "writable": True,
                            "minimum": 0,
                            "maximum": simulator.profile.max_charge_kw,
                            "commands": [
                                "charge_battery",
                                "discharge_battery",
                                "stop_battery",
                            ],
                        }
                    ],
                    "available": True,
                },
            ]
        )

    async def discover(self) -> AdapterSnapshot:
        return self._battery_snapshot.model_copy(
            update={
                "source_states": [
                    {
                        "entity_id": state.source_ref.external_id,
                        "capability": state.capability,
                        "value": state.value,
                        "unit": state.unit,
                        "available": state.status is StateStatus.CURRENT,
                        "observed_at": state.observed_at,
                    }
                    for state in self._states()
                ]
            }
        )

    async def read_state(self, source_refs: Sequence[SourceRef]) -> list[StateSnapshot]:
        wanted = {(ref.adapter_id, ref.external_id) for ref in source_refs}
        return [
            snapshot
            for snapshot in self._states()
            if (snapshot.source_ref.adapter_id, snapshot.source_ref.external_id) in wanted
        ]

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        del execution_context
        self.simulator.command(
            command.command,
            value=command.value if isinstance(command.value, (int, float)) else None,
            idempotency_key=command.idempotency_key,
        )
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id=self.adapter_id, external_id="battery.power"),
            message="battery simulator accepted command",
        )

    def _states(self) -> list[StateSnapshot]:
        state = self.simulator.snapshot()
        received_at = self.clock.now()
        values = {
            "battery.power": (state.power_kw, "kW"),
            "battery.soc": (state.soc_kwh, "kWh"),
            "battery.capacity": (state.capacity_kwh, "kWh"),
        }
        return [
            StateSnapshot(
                device_id="battery.home",
                capability=capability,
                value=value,
                unit=unit,
                observed_at=state.observed_at,
                received_at=received_at,
                status=StateStatus.CURRENT if state.available else StateStatus.UNAVAILABLE,
                source_ref=SourceRef(
                    adapter_id=self.adapter_id,
                    external_id=capability,
                    source_device_id="battery-device",
                ),
            )
            for capability, (value, unit) in values.items()
        ]


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
    assert result.solver_evidence is not None
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


@pytest.mark.composition
def test_hard_battery_min_soc_applies_to_one_slot_terminal_state() -> None:
    horizon = energy_horizon(slots=1, resolution_minutes=60)
    context = energy_context_for(horizon=horizon, with_battery=True)
    assert context.battery is not None
    context = context.model_copy(
        update={
            "base_load_forecast": [BaseLoadPoint(slot=0, power=2.0)],
            "battery": context.battery.model_copy(
                update={
                    "initial_soc_kwh": 5.0,
                    "min_soc_kwh": 0.0,
                    "max_soc_kwh": 6.0,
                    "max_discharge_kw": 2.0,
                    "charge_efficiency": 1.0,
                    "discharge_efficiency": 1.0,
                }
            )
        }
    )
    scenario = OptimizationScenario(
        id="terminal-reserve-one-slot-1",
        horizon=horizon,
        energy_context=context,
        constraints=[
            Constraint(type="max_grid_import", value=0.0, unit="kW", hard=True),
            Constraint(type="battery_min_soc", value=4.0, unit="kWh", hard=True),
        ],
    )

    result = CpSatOptimizer(DeviceRegistry()).optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None


@pytest.mark.composition
@pytest.mark.asyncio
async def test_reconciled_soc_is_carried_into_the_next_horizon() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    horizon = energy_horizon(slots=2, resolution_minutes=60)
    actuator = BatteryActuator(
        device_id="battery.home",
        capability="battery.control",
        charge_command="charge_battery",
        discharge_command="discharge_battery",
        stop_command="stop_battery",
        power_feedback_capability="battery.power",
        power_feedback_tolerance_kw=0.1,
        soc_reconciliation_capability="battery.soc",
    )
    battery_profile = BatterySimulationProfile(
        provider_id="battery_simulator",
        device_id="battery.home",
        capacity_kwh=6.0,
        initial_soc_kwh=2.0,
        min_soc_kwh=1.0,
        max_soc_kwh=5.0,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.9,
    )
    simulator = BatterySimulator(battery_profile, clock=clock)
    adapter = _BatterySimulationAdapter(simulator, clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog(clock=clock)
    await adapter.connect()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()

    context = energy_context_for(horizon=horizon, with_battery=True)
    assert context.battery is not None
    context = context.model_copy(
        update={
            "battery": context.battery.model_copy(
                update={
                    "device_id": actuator.device_id,
                    "initial_soc_kwh": battery_profile.initial_soc_kwh,
                    "min_soc_kwh": battery_profile.min_soc_kwh,
                    "max_soc_kwh": battery_profile.max_soc_kwh,
                    "max_charge_kw": battery_profile.max_charge_kw,
                    "max_discharge_kw": battery_profile.max_discharge_kw,
                    "charge_efficiency": battery_profile.charge_efficiency,
                    "discharge_efficiency": battery_profile.discharge_efficiency,
                    "actuator": actuator,
                }
            )
        }
    )
    policy = TerminalSOCPolicy(minimum_kwh=1.5)
    assert policy.minimum_kwh is not None
    first_scenario = OptimizationScenario(
        id="terminal-next-horizon-first",
        horizon=horizon,
        energy_context=context,
        terminal_soc_policy=policy,
    )
    first_result = CpSatOptimizer(registry).optimize(first_scenario)
    assert first_result.status in {
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL,
        OptimizationStatus.FEASIBLE,
    }, first_result.diagnostics
    assert first_result.objective_values["terminal_soc_kwh"] >= policy.minimum_kwh

    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [],
            RiskClassifier(
                overrides=(
                    RiskOverride(
                        device_id=actuator.device_id,
                        risk_class=RiskClass.SAFE,
                        privileged_exception=True,
                    ),
                )
            ),
        ),
        audit,
        clock=clock,
        authorized_actuator_commands={
            actuator.device_id: frozenset(
                {actuator.charge_command, actuator.stop_command}
            )
        },
    )
    executor = PlanExecutor(adapter, plan_service, audit, clock=clock)
    assert actuator.soc_reconciliation_capability is not None
    charge = plan_service.validate(
        Plan(
            id="terminal-next-horizon-charge",
            commands=[
                Command(
                    id="terminal-next-horizon-charge-command",
                    device_id=actuator.device_id,
                    command=actuator.charge_command,
                    value=1.0,
                    unit="kW",
                    risk_class=RiskClass.SAFE,
                    idempotency_key="terminal-next-horizon-charge-intent",
                    postconditions=[
                        CommandPostcondition(
                            capability=actuator.power_feedback_capability,
                            expected=1.0,
                            tolerance=actuator.power_feedback_tolerance_kw,
                            reconcile_capabilities=[actuator.soc_reconciliation_capability],
                        )
                    ],
                )
            ],
        )
    )
    summary = await executor.execute(charge)
    assert summary.outcomes[0].status.value == "confirmed_success"

    simulator.tick(seconds=3600)
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    reconciled = await state_store.get(actuator.device_id, "battery.soc")
    assert reconciled is not None
    assert reconciled.value == pytest.approx(2.95, abs=0.001)
    assert reconciled.status is StateStatus.CURRENT

    assert isinstance(reconciled.value, (int, float))
    assert context.battery is not None
    next_battery = context.battery.model_copy(
        update={
            "initial_soc_kwh": float(reconciled.value),
            "initial_soc_observation": None,
        }
    )
    next_context = context.model_copy(
        update={
            "battery": next_battery,
            "source_revision": "reconciled-terminal-next-horizon",
            "observed_at": reconciled.observed_at,
        }
    )
    next_result = CpSatOptimizer(registry).optimize(
        OptimizationScenario(
            id="terminal-next-horizon-second",
            horizon=horizon,
            energy_context=next_context,
            terminal_soc_policy=policy,
        )
    )
    assert next_result.status in {
        OptimizationStatus.OPTIMAL_HIERARCHY,
        OptimizationStatus.FEASIBLE_HIERARCHY,
        OptimizationStatus.OPTIMAL,
        OptimizationStatus.FEASIBLE,
    }
    assert next_context.battery is not None
    assert next_context.battery.initial_soc_kwh == pytest.approx(float(reconciled.value))
    assert next_result.objective_values["terminal_soc_kwh"] >= policy.minimum_kwh

    stop = plan_service.validate(
        Plan(
            id="terminal-next-horizon-stop",
            commands=[
                Command(
                    id="terminal-next-horizon-stop-command",
                    device_id=actuator.device_id,
                    command=actuator.stop_command,
                    risk_class=RiskClass.SAFE,
                    idempotency_key="terminal-next-horizon-stop-intent",
                    postconditions=[
                        CommandPostcondition(
                            capability=actuator.power_feedback_capability,
                            expected=0.0,
                            tolerance=actuator.power_feedback_tolerance_kw,
                        )
                    ],
                )
            ],
        )
    )
    stopped = await executor.execute(stop)
    assert stopped.outcomes[0].status.value == "confirmed_success"
