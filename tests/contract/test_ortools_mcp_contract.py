from datetime import UTC, datetime
from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.mcp.ortools_server import OrtoolsMcpContext, create_ortools_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.optimizer.scenario import (
    Constraint,
    Horizon,
    Load,
    OptimizationScenario,
    scenario_definition_digest,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def structured(result: object) -> dict[str, Any]:
    protocol_content = getattr(result, "structuredContent", None)
    if isinstance(protocol_content, dict):
        return protocol_content

    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


async def build_context() -> OrtoolsMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    optimization_service = OptimizationService(
        registry,
        plan_service,
        CpSatOptimizer(registry),
    )
    return OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=optimization_service,
    )


def scenario_for(device_id: str, *, solver_time_limit_seconds: float = 5.0) -> OptimizationScenario:
    return OptimizationScenario(
        id="contract-energy-001",
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
        solver_time_limit_seconds=solver_time_limit_seconds,
    )


@pytest.mark.asyncio
async def test_ortools_mcp_exposes_only_proposal_tools() -> None:
    server = create_ortools_server(await build_context())

    listed_tools = await server.list_tools()
    tools = [tool.name for tool in listed_tools]

    assert tools == [
        "validate_scenario",
        "optimize_scenario",
        "explain_solution",
        "summarize_solution",
        "compare_scenarios",
    ]
    assert "execute_plan" not in tools
    assert "execute_command" not in tools
    assert all(tool.annotations is not None for tool in listed_tools)
    assert all(tool.annotations.readOnlyHint for tool in listed_tools if tool.annotations)
    assert all(not tool.annotations.destructiveHint for tool in listed_tools if tool.annotations)


@pytest.mark.asyncio
async def test_ortools_mcp_validates_and_optimizes_without_adapter_calls() -> None:
    context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    scenario = scenario_for(device_id)

    validation = structured(
        await server.call_tool("validate_scenario", {"scenario": scenario.model_dump(mode="json")})
    )
    result = structured(
        await server.call_tool(
            "optimize_scenario",
            {"scenario": scenario.model_dump(mode="json"), "validate_proposal": True},
        )
    )

    assert validation["valid"] is True
    assert validation["definition_digest"] == scenario_definition_digest(scenario)
    assert result["status"] in {"optimal", "feasible"}
    assert result["definition_digest"] == scenario_definition_digest(scenario)
    assert result["plan"]["status"] in {"ready", "validated"}
    assert result["constraint_summary"]["hard_satisfied"] is True


@pytest.mark.asyncio
async def test_validate_scenario_reports_live_runtime_revision() -> None:
    context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    scenario = scenario_for(device_id)

    first = structured(
        await server.call_tool("validate_scenario", {"scenario": scenario.model_dump(mode="json")})
    )
    second = structured(
        await server.call_tool("validate_scenario", {"scenario": scenario.model_dump(mode="json")})
    )
    assert first["runtime_revision"] == second["runtime_revision"]

    context.plan_service.state_store.begin_revision()

    third = structured(
        await server.call_tool("validate_scenario", {"scenario": scenario.model_dump(mode="json")})
    )
    assert third["runtime_revision"] != second["runtime_revision"]


@pytest.mark.asyncio
async def test_explain_solution_returns_versioned_proposal_projection() -> None:
    context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    scenario = scenario_for(device_id)

    result = structured(
        await server.call_tool("optimize_scenario", {"scenario": scenario.model_dump(mode="json")})
    )
    explanation = structured(await server.call_tool("explain_solution", {"result": result}))

    assert explanation["schema_version"] == "v1"
    assert explanation["scenario_id"] == scenario.id
    assert explanation["proposal"]["plan_id"] == result["plan"]["id"]
    assert explanation["constraint_summary"]["hard_satisfied"] is True
    assert explanation["alternatives"]
    assert all(
        set(alternative)
        >= {
            "plan_id",
            "status",
            "objective_values",
            "constraint_effects",
            "forecast_assumptions",
        }
        for alternative in explanation["alternatives"]
    )


@pytest.mark.asyncio
async def test_summarize_solution_returns_product_projection_without_commands() -> None:
    context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    scenario = scenario_for(device_id)
    result = structured(
        await server.call_tool("optimize_scenario", {"scenario": scenario.model_dump(mode="json")})
    )

    summary = structured(await server.call_tool("summarize_solution", {"result": result}))

    assert summary["schema_version"] == "v1"
    assert summary["scenario_id"] == scenario.id
    assert summary["proposal_id"] == result["plan"]["id"]
    assert "commands" not in summary


@pytest.mark.asyncio
async def test_compare_scenarios_returns_read_only_product_diffs() -> None:
    context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    baseline = scenario_for(device_id)
    variation = baseline.model_copy(update={"id": "contract-energy-variation"})

    comparison = structured(
        await server.call_tool(
            "compare_scenarios",
            {
                "baseline": baseline.model_dump(mode="json"),
                "variations": {"same_inputs": variation.model_dump(mode="json")},
            },
        )
    )

    assert comparison["schema_version"] == "v1"
    assert comparison["baseline"]["scenario_id"] == baseline.id
    assert comparison["variations"]["same_inputs"]["scenario_id"] == variation.id
    assert comparison["variations"]["same_inputs"]["diff"]["start_slot_sum"] == pytest.approx(0)
    assert "commands" not in comparison["baseline"]


@pytest.mark.asyncio
async def test_ortools_mcp_returns_safe_error_for_malformed_input() -> None:
    server = create_ortools_server(await build_context())

    result = structured(await server.call_tool("validate_scenario", {"scenario": {}}))

    assert result["error"]["code"] == "validation_error"
    assert "token" not in str(result).lower()
