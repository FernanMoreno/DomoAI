"""Local stdio entry point backed by the deterministic simulated home."""

from __future__ import annotations

import asyncio

from mcp.server.fastmcp import FastMCP

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.mcp.configured import build_configured_server
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def build_fixture_server() -> FastMCP:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=plan_service.policy_engine.policies,
        active_provider_ids=(adapter.adapter_id,),
    )
    optimizer_context = OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=OptimizationService(
            registry,
            plan_service,
            CpSatOptimizer(registry),
        ),
    )
    return create_unified_server(UnifiedMcpContext(domotics=context, optimizer=optimizer_context))


async def run_stdio() -> None:
    runtime, server = await build_configured_server()
    await runtime.start()
    try:
        await server.run_stdio_async()
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(run_stdio())
