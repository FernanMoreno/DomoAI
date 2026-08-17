"""stdio entry point for the proposal-only OR-Tools MCP server."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from mcp.server.fastmcp import FastMCP

from domoai.application.optimization_service import OptimizationService
from domoai.application.runtime_factory import RuntimeComposition, build_runtime
from domoai.config.settings import Settings
from domoai.mcp.ortools_server import OrtoolsMcpContext, create_ortools_server
from domoai.optimizer.cp_sat import CpSatOptimizer


async def build_configured_server(
    settings: Settings | None = None,
) -> tuple[RuntimeComposition, FastMCP]:
    """Build the optimizer server with the fixture or configured runtime."""

    runtime = await build_runtime(settings)
    optimization_service = OptimizationService(
        runtime.registry,
        runtime.plan_service,
        CpSatOptimizer(runtime.registry),
    )
    context = OrtoolsMcpContext(
        registry=runtime.registry,
        plan_service=runtime.plan_service,
        optimization_service=optimization_service,
        runtime_revision=runtime.plan_service.current_revision,
    )
    return runtime, create_ortools_server(context)


async def run_stdio() -> None:
    """Serve optimizer tools over MCP stdio until the host disconnects."""

    runtime, server = await build_configured_server()
    event_task = asyncio.create_task(runtime.event_consumer.run())
    try:
        await server.run_stdio_async()
    finally:
        event_task.cancel()
        with suppress(asyncio.CancelledError):
            await event_task
        await runtime.close()


def main() -> None:
    asyncio.run(run_stdio())
