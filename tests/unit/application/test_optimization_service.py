import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan
from domoai.optimizer.ports import OptimizationResult, OptimizationStatus
from domoai.optimizer.scenario import OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.policy_engine import PolicyEngine
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
