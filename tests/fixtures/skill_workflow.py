"""Deterministic MCP fixtures for the portable energy skill workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.state_service import StateService
from domoai.domain.models import Policy, PolicyAction
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.energy import StaticEnergyContextProvider
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from domoai.skills.workflow import ApprovalDecision
from tests.fixtures.energy import energy_context_for


def structured(result: object) -> dict[str, Any]:
    """Normalize FastMCP's in-process result shape for the router."""

    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


@dataclass
class FixtureApprovalPort:
    decisions: list[ApprovalDecision | None] = field(default_factory=list)
    requests: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)

    async def request_approval(
        self, plan: dict[str, Any], explanation: dict[str, Any]
    ) -> ApprovalDecision | None:
        self.requests.append((plan, explanation))
        if self.decisions:
            return self.decisions.pop(0)
        return None


@dataclass
class FixtureToolRouter:
    unified_server: Any
    domotics_context: DomoticsMcpContext
    ortools_context: OrtoolsMcpContext
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    tool_aliases: dict[tuple[str, str], str] = field(default_factory=dict)

    async def call(
        self, provider: str, tool: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((provider, tool, arguments))
        actual_tool = self.tool_aliases.get((provider, tool), tool)
        if provider != "mcp":
            raise AssertionError(f"Unexpected provider role: {provider}")
        result = await self.unified_server.call_tool(actual_tool, arguments)
        return structured(result)

    def current_revision(self, provider: str) -> str | None:
        if provider == "mcp":
            return self.domotics_context.facade.plan_service.current_revision
        return None


@dataclass
class WorkflowFixture:
    domotics_adapter: SimulatedHomeAdapter
    domotics_context: DomoticsMcpContext
    ortools_context: OrtoolsMcpContext
    router: FixtureToolRouter
    approval: FixtureApprovalPort


async def build_workflow_fixture(
    *,
    confirmation_required: bool = False,
    approval_decisions: list[ApprovalDecision | None] | None = None,
    tool_aliases: dict[tuple[str, str], str] | None = None,
) -> WorkflowFixture:
    domotics_adapter, domotics_context = await _build_domotics_context(
        confirmation_required=confirmation_required
    )
    ortools_context = OrtoolsMcpContext(
        registry=domotics_context.registry,
        plan_service=domotics_context.facade.plan_service,
        optimization_service=OptimizationService(
            domotics_context.registry,
            domotics_context.facade.plan_service,
            CpSatOptimizer(domotics_context.registry),
        ),
        runtime_revision=domotics_context.facade.plan_service.current_revision,
    )
    unified_context = UnifiedMcpContext(
        domotics=domotics_context,
        optimizer=ortools_context,
    )
    approval = FixtureApprovalPort(decisions=list(approval_decisions or []))
    router = FixtureToolRouter(
        unified_server=create_unified_server(unified_context),
        domotics_context=domotics_context,
        ortools_context=ortools_context,
        tool_aliases=tool_aliases or {},
    )
    return WorkflowFixture(
        domotics_adapter=domotics_adapter,
        domotics_context=domotics_context,
        ortools_context=ortools_context,
        router=router,
        approval=approval,
    )


async def _build_domotics_context(
    *, confirmation_required: bool
) -> tuple[SimulatedHomeAdapter, DomoticsMcpContext]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    policies = (
        [
            Policy(
                id="fixture-confirm-brightness",
                target={"capability": "brightness"},
                action=PolicyAction.CONFIRM,
            )
        ]
        if confirmation_required
        else []
    )
    plan_service = PlanService(registry, state_store, PolicyEngine(policies), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return adapter, DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=policies,
        energy_context_provider=StaticEnergyContextProvider(
            energy_context_for(
                Horizon(
                    start=datetime(2026, 8, 15, tzinfo=UTC),
                    end=datetime(2026, 8, 15, 1, tzinfo=UTC),
                    resolution_minutes=15,
                    timezone="Europe/Madrid",
                )
            )
        ),
    )


def scenario_for(
    device_id: str,
    *,
    max_power: float = 500,
    solver_time_limit_seconds: float = 5.0,
) -> OptimizationScenario:
    return OptimizationScenario(
        id="fixture-energy-001",
        horizon=Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 1, tzinfo=UTC),
            resolution_minutes=15,
            timezone="Europe/Madrid",
        ),
        loads=[
            Load(
                id="light-load",
                device_id=device_id,
                capability="brightness",
                command="set_brightness",
                value=60,
                unit="%",
                power=100,
                power_unit="W",
            )
        ],
        constraints=[Constraint(type="max_house_power", value=max_power, unit="W")],
        solver_time_limit_seconds=solver_time_limit_seconds,
    )


def light_device_id(fixture: WorkflowFixture) -> str:
    return next(
        device.id
        for device in fixture.domotics_context.registry.devices
        if device.type.value == "light"
    )
