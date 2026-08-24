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
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


async def build_context() -> tuple[SimulatedHomeAdapter, OrtoolsMcpContext]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    context = OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=OptimizationService(
            registry,
            plan_service,
            CpSatOptimizer(registry),
        ),
    )
    return adapter, context


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


@pytest.mark.asyncio
async def test_feasible_fixture_returns_proposal_without_adapter_calls() -> None:
    adapter, context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )

    result = structured(
        await server.call_tool(
            "optimize_scenario",
            {"scenario": scenario_for(device_id).model_dump(mode="json")},
        )
    )

    assert result["status"] in {"optimal", "feasible"}
    assert result["plan"]["commands"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_invalid_fixture_returns_diagnostics_before_solver_or_adapter() -> None:
    adapter, context = await build_context()
    server = create_ortools_server(context)
    scenario = scenario_for("unknown.device")

    result = structured(
        await server.call_tool("optimize_scenario", {"scenario": scenario.model_dump(mode="json")})
    )

    assert result["status"] == "invalid"
    assert result["diagnostics"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_infeasible_and_timeout_fixtures_are_not_successes() -> None:
    adapter, context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )

    infeasible = structured(
        await server.call_tool(
            "optimize_scenario",
            {"scenario": scenario_for(device_id, max_power=50).model_dump(mode="json")},
        )
    )
    timeout = structured(
        await server.call_tool(
            "optimize_scenario",
            {
                "scenario": scenario_for(device_id, solver_time_limit_seconds=0).model_dump(
                    mode="json"
                )
            },
        )
    )

    assert infeasible["status"] == "infeasible"
    assert infeasible["diagnostics"]
    assert timeout["status"] == "timeout"
    assert timeout["diagnostics"]
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_proposal_validation_reuses_runtime_boundary_without_execution() -> None:
    adapter, context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    scenario = scenario_for(device_id)

    result = structured(
        await server.call_tool(
            "optimize_scenario",
            {
                "scenario": scenario.model_dump(mode="json"),
                "validate_proposal": True,
            },
        )
    )

    assert result["plan"]["validation"]["status"] == "valid"
    assert result["plan"]["status"] == "ready"
    assert adapter.calls == []
