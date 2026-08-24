import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, Plan
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class _FixedOptimizer:
    def __init__(self, result: OptimizationResult) -> None:
        self._result = result

    def optimize(self, scenario: OptimizationScenario) -> OptimizationResult:
        del scenario
        return self._result


async def build_service() -> tuple[DeviceRegistry, PlanService]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), AuditLog())
    return registry, plan_service


def _plan(plan_id: str, device_id: str, command_id: str) -> Plan:
    return Plan(
        id=plan_id,
        commands=[
            Command(
                id=command_id,
                device_id=device_id,
                command="turn_on",
                idempotency_key=f"intent-{command_id}",
            )
        ],
    )


@pytest.mark.asyncio
async def test_validate_proposal_validates_every_bundle_member() -> None:
    registry, plan_service = await build_service()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plans = [
        _plan("bundle-plan-0", device_id, "bundle-cmd-0"),
        _plan("bundle-plan-1", device_id, "bundle-cmd-1"),
    ]
    result = OptimizationResult(
        scenario_id="fixture-scenario-1",
        status=OptimizationStatus.OPTIMAL,
        solver="fake",
        plan=plans[0],
        plans=plans,
    )
    service = OptimizationService(registry, plan_service, _FixedOptimizer(result))

    validated = service.validate_proposal(result)

    assert len(validated.plans) == 2
    assert all(plan.validation is not None for plan in validated.plans)
    assert validated.plan == validated.plans[0]


@pytest.mark.asyncio
async def test_validate_proposal_falls_back_to_plan_only_when_plans_is_empty() -> None:
    registry, plan_service = await build_service()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = _plan("solo-plan-1", device_id, "solo-cmd-1")
    result = OptimizationResult(
        scenario_id="fixture-scenario-1",
        status=OptimizationStatus.OPTIMAL,
        solver="fake",
        plan=plan,
        plans=[],
    )
    service = OptimizationService(registry, plan_service, _FixedOptimizer(result))

    validated = service.validate_proposal(result)

    assert len(validated.plans) == 1
    assert validated.plans[0].validation is not None
    assert validated.plan == validated.plans[0]


def _result_with_wall_time(wall_time_seconds: float) -> OptimizationResult:
    from domoai.optimizer.ports import SolverEvidence

    return OptimizationResult(
        scenario_id="fixture-scenario-latency",
        status=OptimizationStatus.OPTIMAL,
        solver="fake",
        solver_evidence=SolverEvidence(
            solver_name="cp-sat",
            solver_version="test",
            num_search_workers=1,
            random_seed=0,
            wall_time_seconds=wall_time_seconds,
            tiers=[],
            scenario_fingerprint="fp",
        ),
    )


@pytest.mark.asyncio
async def test_last_wall_time_seconds_none_before_any_optimize_call() -> None:
    registry, plan_service = await build_service()
    service = OptimizationService(
        registry, plan_service, _FixedOptimizer(_result_with_wall_time(1.0))
    )

    assert service.last_wall_time_seconds is None


@pytest.mark.asyncio
async def test_last_wall_time_seconds_set_after_one_call_and_updated_after_a_second() -> None:
    registry, plan_service = await build_service()
    service = OptimizationService(
        registry, plan_service, _FixedOptimizer(_result_with_wall_time(1.5))
    )
    scenario = object()

    service.optimize(scenario)  # type: ignore[arg-type]
    assert service.last_wall_time_seconds == 1.5

    service.optimizer = _FixedOptimizer(_result_with_wall_time(2.5))
    service.optimize(scenario)  # type: ignore[arg-type]
    assert service.last_wall_time_seconds == 2.5


@pytest.mark.asyncio
async def test_validate_proposal_rejects_nonzero_unbound_battery_dispatch() -> None:
    registry, plan_service = await build_service()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = _plan("battery-unbound-plan", device_id, "battery-unbound-command")
    result = OptimizationResult(
        scenario_id="battery-unbound-scenario",
        status=OptimizationStatus.OPTIMAL,
        solver="fake",
        plan=plan,
        plans=[plan],
        constraint_summary={
            "slots": [
                {
                    "slot": 0,
                    "battery_charge_kw": 1.0,
                    "battery_discharge_kw": 0.0,
                }
            ]
        },
    )
    service = OptimizationService(registry, plan_service, _FixedOptimizer(result))

    validated = service.validate_proposal(result)

    assert validated.status is OptimizationStatus.INVALID
    assert validated.plan is None
    assert validated.plans == []
    assert [item.code for item in validated.diagnostics] == ["battery_actuation_unbound"]


@pytest.mark.asyncio
async def test_validate_proposal_allows_zero_battery_dispatch() -> None:
    registry, plan_service = await build_service()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = _plan("battery-zero-plan", device_id, "battery-zero-command")
    result = OptimizationResult(
        scenario_id="battery-zero-scenario",
        status=OptimizationStatus.OPTIMAL,
        solver="fake",
        plan=plan,
        plans=[plan],
        constraint_summary={
            "slots": [
                {
                    "slot": 0,
                    "battery_charge_kw": 0.0,
                    "battery_discharge_kw": 0.0,
                }
            ]
        },
    )
    service = OptimizationService(registry, plan_service, _FixedOptimizer(result))

    validated = service.validate_proposal(result)

    assert validated.status is OptimizationStatus.OPTIMAL
    assert len(validated.plans) == 1
    assert validated.plans[0].validation is not None
