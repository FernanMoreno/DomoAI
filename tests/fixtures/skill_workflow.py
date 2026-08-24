"""Deterministic MCP fixtures for the portable energy skill workflow."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from domoai.persistence.repositories import BundleCommitRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.bundle_commit import BundleCommitService
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore
from domoai.skills.workflow import ApprovalDecision
from tests.fixtures.energy import energy_context_for

FIXTURE_OPERATOR_TOKEN = "fixture-operator-secret"


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

    async def call(self, provider: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
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
    horizon: Horizon | None = None,
    clock: Clock | None = None,
) -> WorkflowFixture:
    domotics_adapter, domotics_context = await _build_domotics_context(
        confirmation_required=confirmation_required, horizon=horizon, clock=clock
    )
    ortools_context = OrtoolsMcpContext(
        registry=domotics_context.registry,
        plan_service=domotics_context.facade.plan_service,
        optimization_service=OptimizationService(
            domotics_context.registry,
            domotics_context.facade.plan_service,
            CpSatOptimizer(domotics_context.registry),
        ),
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


def default_horizon() -> Horizon:
    return Horizon(
        start=datetime(2026, 8, 15, tzinfo=UTC),
        end=datetime(2026, 8, 15, 1, tzinfo=UTC),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )


def future_horizon(*, slots: int = 4, resolution_minutes: int = 15) -> Horizon:
    """A horizon straddling real "now" — required to genuinely exercise the
    schedule_plan path, since every fixture using default_horizon() is fixed
    at 2026-08-15 and would take the execute-now branch unconditionally
    regardless of whether scheduling is implemented correctly."""

    start = datetime.now(UTC) - timedelta(minutes=resolution_minutes)
    return Horizon(
        start=start,
        end=start + timedelta(minutes=slots * resolution_minutes),
        resolution_minutes=resolution_minutes,
        timezone="Europe/Madrid",
    )


async def _build_domotics_context(
    *, confirmation_required: bool, horizon: Horizon | None = None, clock: Clock | None = None
) -> tuple[SimulatedHomeAdapter, DomoticsMcpContext]:
    runtime_clock = clock or SystemClock()
    adapter = SimulatedHomeAdapter(clock=runtime_clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=runtime_clock)
    audit = AuditLog(clock=runtime_clock)
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    policies = (
        [
            Policy(
                id="fixture-confirm-brightness",
                target={"capability": "brightness"},
                action=PolicyAction.CONFIRM,
            ),
            Policy(
                id="fixture-confirm-power",
                target={"capability": "power"},
                action=PolicyAction.CONFIRM,
            ),
        ]
        if confirmation_required
        else []
    )
    database = SQLiteDatabase(
        Path(tempfile.mkdtemp()) / "scheduler-fixture.sqlite3", clock=runtime_clock
    )
    await database.initialize()
    plan_service = PlanService(
        registry, state_store, PolicyEngine(policies), audit, clock=runtime_clock
    )
    executor = PlanExecutor(adapter, plan_service, audit, clock=runtime_clock)
    facade = DomoticsFacade(plan_service, executor)
    scheduled_repository = ScheduledPlanRepository(database, clock=runtime_clock)
    bundle_repository = BundleCommitRepository(database, clock=runtime_clock)
    scheduler = Scheduler(
        executor,
        scheduled_repository,
        audit,
        bundle_repository=bundle_repository,
        clock=runtime_clock,
    )
    approval_store = ApprovalStore(
        operator_token=FIXTURE_OPERATOR_TOKEN,
        allow_legacy_token=True,
        clock=runtime_clock,
    )
    plans: dict[str, Any] = {}
    bundle_commit_service = BundleCommitService(
        facade=facade,
        plans=plans,
        approval_store=approval_store,
        bundle_repository=bundle_repository,
        scheduled_repository=scheduled_repository,
        audit=audit,
        clock=runtime_clock,
    )
    return adapter, DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=policies,
        approval_store=approval_store,
        plans=plans,
        bundle_commit_service=bundle_commit_service,
        energy_context_provider=StaticEnergyContextProvider(
            energy_context_for(horizon or default_horizon(), with_battery=False)
        ),
        scheduler=scheduler,
        clock=runtime_clock,
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


def multi_slot_scenario_for(
    device_id: str,
    *,
    horizon: Horizon,
    slots: tuple[int, ...] = (0, 2),
    device_ids: tuple[str, ...] | None = None,
    capability: str = "power",
    command: str = "turn_on",
    value: Any = True,
    max_power: float = 500,
    solver_time_limit_seconds: float = 5.0,
    invalid_device_id: str | None = None,
) -> OptimizationScenario:
    """A scenario whose loads are pinned (earliest_slot == latest_slot) to
    distinct slots, so CP-SAT is forced to produce one Plan per slot instead
    of leaving slot assignment to solver heuristics. `device_ids`, if given,
    assigns one distinct device per slot (avoiding a same-device dependency
    conflict between bundle members when an earlier member's execution
    changes the state a later member's validation depended on) — defaults to
    repeating `device_id` for every slot when omitted. `invalid_device_id`,
    if given, replaces the device on the *last* load only — used to engineer
    a bundle whose later member fails validation."""

    resolved_devices = device_ids or tuple(device_id for _ in slots)
    return OptimizationScenario(
        id="fixture-energy-bundle-001",
        horizon=horizon,
        loads=[
            Load(
                id=f"bundle-load-{index}",
                device_id=(
                    invalid_device_id
                    if invalid_device_id is not None and index == len(slots) - 1
                    else resolved_devices[index]
                ),
                capability=capability,
                command=command,
                value=value,
                power=100,
                power_unit="W",
                duration_slots=1,
                earliest_slot=slot,
                latest_slot=slot,
            )
            for index, slot in enumerate(slots)
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


def switch_device_id(fixture: WorkflowFixture) -> str:
    return next(
        device.id
        for device in fixture.domotics_context.registry.devices
        if device.type.value == "switch"
    )
