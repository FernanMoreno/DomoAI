"""Local stdio entry point backed by the deterministic simulated home."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from mcp.server.fastmcp import FastMCP

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.optimization_service import OptimizationService
from domoai.application.plan_service import PlanService
from domoai.application.runtime_factory import RuntimeComposition, build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.mcp.domotics_server import DomoticsMcpContext
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
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
        policies=[],
    )
    optimizer_context = OrtoolsMcpContext(
        registry=registry,
        plan_service=plan_service,
        optimization_service=OptimizationService(
            registry,
            plan_service,
            CpSatOptimizer(registry),
        ),
        runtime_revision=plan_service.current_revision,
    )
    return create_unified_server(
        UnifiedMcpContext(domotics=context, optimizer=optimizer_context)
    )


async def build_configured_server(
    settings: Settings | None = None,
) -> tuple[RuntimeComposition, FastMCP]:
    runtime = await build_runtime(settings)
    context = DomoticsMcpContext(
        discovery=runtime.discovery,
        state_service=StateService(runtime.state_store),
        facade=runtime.facade,
        registry=runtime.registry,
        policies=[],
        energy_context_provider=runtime.energy_context_provider,
        scheduler=runtime.scheduler,
    )
    optimizer_context = OrtoolsMcpContext(
        registry=runtime.registry,
        plan_service=runtime.plan_service,
        optimization_service=OptimizationService(
            runtime.registry,
            runtime.plan_service,
            CpSatOptimizer(runtime.registry),
        ),
        runtime_revision=runtime.plan_service.current_revision,
    )
    return runtime, create_unified_server(
        UnifiedMcpContext(domotics=context, optimizer=optimizer_context)
    )


async def run_stdio() -> None:
    runtime, server = await build_configured_server()
    event_task = asyncio.create_task(runtime.event_consumer.run())
    scheduler_task = asyncio.create_task(runtime.scheduler.run())
    try:
        await server.run_stdio_async()
    finally:
        event_task.cancel()
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await event_task
        with suppress(asyncio.CancelledError):
            await scheduler_task
        await runtime.close()


def main() -> None:
    asyncio.run(run_stdio())
