from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.domain.models import PlanStatus
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def build_optimization_context() -> tuple[
    SimulatedHomeAdapter, DeviceRegistry, OptimizationService
]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    service = OptimizationService(registry, plan_service, CpSatOptimizer(registry))
    return adapter, registry, service


def horizon(hours: int = 4) -> Horizon:
    return Horizon(
        start=datetime(2026, 8, 15, tzinfo=UTC),
        end=datetime(2026, 8, 15, hours, tzinfo=UTC),
        resolution_minutes=60,
        timezone="Europe/Madrid",
    )


@pytest.mark.asyncio
async def test_feasible_schedule_returns_proposal_without_adapter_side_effect() -> None:
    adapter, registry, service = await build_optimization_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    scenario = OptimizationScenario(
        id="scenario-feasible-1",
        horizon=horizon(),
        loads=[
            Load(
                id="load-pump-1",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=1000,
                power_unit="W",
                earliest_slot=1,
                latest_slot=2,
            )
        ],
        constraints=[Constraint(type="max_house_power", value=1500, unit="W")],
    )

    result = service.optimize(scenario)

    assert result.status in {OptimizationStatus.FEASIBLE, OptimizationStatus.OPTIMAL}
    assert result.plan is not None
    assert result.plan.status is PlanStatus.DRAFT
    assert adapter.calls == []
    assert result.plan.commands[0].intent == "scheduled_slot:1"

    validated = service.validate_proposal(result)

    assert validated.plan is not None
    assert validated.plan.status is PlanStatus.READY
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_conflicting_hard_power_limit_returns_explanation() -> None:
    _, registry, service = await build_optimization_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    scenario = OptimizationScenario(
        id="scenario-infeasible-1",
        horizon=horizon(2),
        loads=[
            Load(
                id="load-pump-2",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=2000,
                power_unit="W",
                earliest_slot=0,
                latest_slot=0,
            )
        ],
        constraints=[Constraint(type="max_house_power", value=1000, unit="W")],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INFEASIBLE
    assert result.plan is None
    assert any("power" in diagnostic.message.lower() for diagnostic in result.diagnostics)


@pytest.mark.asyncio
async def test_missing_capability_and_invalid_unit_are_rejected_before_solver() -> None:
    _, registry, service = await build_optimization_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    scenario = OptimizationScenario(
        id="scenario-invalid-1",
        horizon=horizon(),
        loads=[
            Load(
                id="load-invalid-1",
                device_id=device_id,
                capability="brightness",
                command="set_brightness",
                value=60,
                unit="%",
                power=1,
                power_unit="MW",
            )
        ],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.INVALID
    assert result.plan is None
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "missing_capability",
        "invalid_unit",
    }


@pytest.mark.asyncio
async def test_zero_solver_budget_returns_timeout_without_adapter_side_effect() -> None:
    adapter, registry, service = await build_optimization_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    scenario = OptimizationScenario(
        id="scenario-timeout-1",
        horizon=horizon(),
        solver_time_limit_seconds=0,
        loads=[
            Load(
                id="load-timeout-1",
                device_id=device_id,
                capability="power",
                command="turn_on",
                value=True,
                power=1000,
                power_unit="W",
            )
        ],
    )

    result = service.optimize(scenario)

    assert result.status is OptimizationStatus.TIMEOUT
    assert result.plan is None
    assert adapter.calls == []
