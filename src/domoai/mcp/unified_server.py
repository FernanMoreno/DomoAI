"""One general MCP surface for the complete DomoAI runtime."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from domoai.mcp.compat import ensure_fastmcp_settings_ready
from domoai.mcp.domotics_server import (
    DomoticsMcpContext,
    register_domotics_tools,
)
from domoai.mcp.ortools_server import (
    OrtoolsMcpContext,
    register_ortools_tools,
)


@dataclass(frozen=True)
class UnifiedMcpContext:
    """Two internal views over one runtime, registry and plan boundary."""

    domotics: DomoticsMcpContext
    optimizer: OrtoolsMcpContext

    def __post_init__(self) -> None:
        if self.domotics.registry is not self.optimizer.registry:
            raise ValueError("unified MCP contexts must share one device registry")
        if self.domotics.facade.plan_service is not self.optimizer.plan_service:
            raise ValueError("unified MCP contexts must share one plan service")


def create_unified_server(context: UnifiedMcpContext) -> FastMCP:
    """Create the sole public MCP server and register every semantic surface."""

    ensure_fastmcp_settings_ready()
    server = FastMCP(
        "DomoAI",
        instructions=(
            "General semantic smart-home operating layer. Discovery, state, "
            "policy, runtime execution and proposal-only OR-Tools optimization "
            "are available through this one MCP connection."
        ),
    )
    register_domotics_tools(server, context.domotics)
    register_ortools_tools(server, context.optimizer)
    return server
