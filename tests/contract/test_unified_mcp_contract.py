from datetime import UTC, datetime
from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.state_service import StateService
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.stdio import build_fixture_server
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


async def build_context() -> tuple[SimulatedHomeAdapter, UnifiedMcpContext]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    domotics = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
    )
    optimizer = OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=OptimizationService(
            registry,
            plan_service,
            CpSatOptimizer(registry),
        ),
        runtime_revision=plan_service.current_revision,
    )
    return adapter, UnifiedMcpContext(domotics=domotics, optimizer=optimizer)


def scenario_for(device_id: str) -> OptimizationScenario:
    return OptimizationScenario(
        id="unified-contract-energy-001",
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
        constraints=[Constraint(type="max_house_power", value=500, unit="W")],
        solver_time_limit_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_unified_server_exposes_one_complete_semantic_catalog() -> None:
    _, context = await build_context()
    server = create_unified_server(context)

    tools = await server.list_tools()
    assert [tool.name for tool in tools] == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "validate_command",
        "validate_plan",
        "request_approval",
        "execute_plan",
        "schedule_plan",
        "cancel_scheduled_plan",
        "reschedule_plan",
        "list_scheduled_plans",
        "validate_scenario",
        "optimize_scenario",
        "explain_solution",
    ]
    annotations = {tool.name: tool.annotations for tool in tools}
    assert all(annotation is not None for annotation in annotations.values())
    assert annotations["execute_plan"].destructiveHint is True  # type: ignore[union-attr]
    assert annotations["optimize_scenario"].readOnlyHint is True  # type: ignore[union-attr]
    assert [str(resource.uri) for resource in await server.list_resources()] == [
        "domotics://areas",
        "domotics://capabilities",
        "domotics://devices",
        "domotics://energy",
        "domotics://policies",
    ]


@pytest.mark.asyncio
async def test_public_fixture_entrypoint_uses_the_unified_server() -> None:
    server = await build_fixture_server()

    assert {tool.name for tool in await server.list_tools()} >= {
        "discover_devices",
        "optimize_scenario",
        "execute_plan",
    }


@pytest.mark.asyncio
async def test_unified_session_optimizes_without_execution_authority() -> None:
    adapter, context = await build_context()
    server = create_unified_server(context)
    device_id = next(
        device.id for device in context.domotics.registry.devices if device.type.value == "light"
    )
    scenario = scenario_for(device_id).model_dump(mode="json")

    discovery = structured(await server.call_tool("discover_devices", {"refresh": False}))
    validation = structured(await server.call_tool("validate_scenario", {"scenario": scenario}))
    proposal = structured(
        await server.call_tool(
            "optimize_scenario",
            {"scenario": scenario, "validate_proposal": True},
        )
    )
    explanation = structured(
        await server.call_tool("explain_solution", {"result": proposal})
    )

    assert discovery["devices"]
    assert validation["valid"] is True
    assert proposal["status"] in {"optimal", "feasible"}
    assert explanation["proposal"]["plan_id"] == proposal["plan"]["id"]
    assert adapter.calls == []
    assert "execute_plan" not in {"validate_scenario", "optimize_scenario", "explain_solution"}
