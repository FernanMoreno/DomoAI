from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.gateway import create_gateway_server
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_gateway_contract_exposes_one_unified_semantic_catalog(tmp_path: Path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    domotics = DomoticsMcpContext(
        discovery=DiscoveryService(adapter, registry, state_store, audit),
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
    )
    optimizer = OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=OptimizationService(registry, plan_service, CpSatOptimizer(registry)),
    )
    server = create_gateway_server(
        UnifiedMcpContext(domotics=domotics, optimizer=optimizer),
        Settings(
            database_path=tmp_path / "gateway.sqlite3",
            mcp_path="/mcp",
            mcp_public_url="http://127.0.0.1:8000",
        ),
    )

    tool_names = {tool.name for tool in await server.list_tools()}

    assert server.settings.streamable_http_path == "/mcp"
    assert {
        "discover_devices",
        "get_state",
        "validate_plan",
        "request_approval",
        "execute_plan",
        "optimize_scenario",
        "list_audit_events",
    } <= tool_names
