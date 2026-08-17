
import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.domain.models import PlanStatus
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, flexible_load


async def build_context() -> tuple[SimulatedHomeAdapter, DeviceRegistry, OptimizationService]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    return adapter, registry, OptimizationService(registry, plan_service, CpSatOptimizer(registry))


@pytest.mark.asyncio
async def test_energy_optimizer_returns_balanced_proposal_with_storage_evidence() -> None:
    adapter, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for()
    scenario = OptimizationScenario(
        id="energy-feasible-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=1.5, latest_slot=5)],
        constraints=[
            Constraint(type="max_house_power", value=3, unit="kW"),
            Constraint(type="max_grid_import", value=3, unit="kW"),
            Constraint(type="max_grid_export", value=3, unit="kW"),
        ],
        objectives=[
            Objective(name="minimize_energy_cost", direction="minimize"),
            Objective(name="maximize_solar_self_consumption", direction="maximize"),
        ],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
    assert result.plan.status is PlanStatus.DRAFT
    assert adapter.calls == []
    assert result.constraint_summary["hard_satisfied"] is True
    assert len(result.constraint_summary["slots"]) == context.horizon.slots
    assert result.objective_values["energy_cost"] >= 0
    assert all(
        abs(
            slot["load_power_kw"] + slot["battery_charge_kw"]
            - slot["solar_power_kw"]
            - slot["grid_import_kw"]
            - slot["battery_discharge_kw"]
            + slot["grid_export_kw"]
        )
        < 0.00001
        for slot in result.constraint_summary["slots"]
    )


@pytest.mark.asyncio
async def test_energy_optimizer_rejects_impossible_grid_limit_without_plan() -> None:
    _, registry, service = await build_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    context = energy_context_for(with_battery=False)
    scenario = OptimizationScenario(
        id="energy-infeasible-1",
        horizon=context.horizon,
        energy_context=context,
        loads=[flexible_load(device_id, power_kw=2, earliest_slot=0, latest_slot=0)],
        constraints=[Constraint(type="max_grid_import", value=0, unit="kW")],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None
    assert result.diagnostics
