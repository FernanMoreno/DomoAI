"""Network entrypoint for the one shared DomoAI MCP runtime."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.types import Receive, Scope, Send

from domoai.application.runtime_factory import RuntimeComposition
from domoai.config.settings import Settings
from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from domoai.runtime.approval_store import (
    OperatorApprovalAssertionProvider,
    OperatorPrincipalProvider,
)


def create_gateway_server(
    context: UnifiedMcpContext,
    settings: Settings,
    *,
    runtime: Any | None = None,
) -> FastMCP:
    """Create the configured network server over an already-built runtime."""

    return create_unified_server(context, settings=settings, runtime=runtime)


@dataclass
class GatewayApplication:
    """Own the network app and the runtime lifecycle as one unit."""

    runtime: RuntimeComposition
    server: FastMCP
    _app: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        app = self.server.streamable_http_app()
        if not self.runtime.settings.mcp_server_sent_events:
            app.add_middleware(
                _RejectServerSentEventsMiddleware,
                mcp_path=self.runtime.settings.mcp_path,
            )
        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def managed_lifespan(starlette_app: Any) -> AsyncIterator[None]:
            async with original_lifespan(starlette_app):
                try:
                    yield
                finally:
                    # FastMCP's session manager cancels its task group and
                    # clears its transport table during lifespan exit. Close
                    # active transports first so SSE responses release their
                    # server-side memory streams before that table disappears.
                    await self.close_http_sessions()

        app.router.lifespan_context = managed_lifespan
        self._app = app

    @property
    def app(self) -> Any:
        return self._app

    async def start(self) -> None:
        await self.runtime.start()

    async def close(self) -> None:
        await self.close_http_sessions()
        await self.runtime.close()

    async def close_http_sessions(self) -> None:
        """Terminate active stateful MCP sessions before stopping the runtime."""

        await _terminate_active_http_sessions(self.server)

    @asynccontextmanager
    async def http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Provide an in-process client for contract/composition tests."""

        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=self.runtime.settings.mcp_public_url,
        ) as client:
            yield client


async def _terminate_active_http_sessions(server: FastMCP) -> None:
    """Terminate stateful MCP sessions before the SDK manager is cancelled.

    MCP 1.x does not expose a public session-manager shutdown method. The
    Streamable HTTP app does expose the manager, and its active transport table
    is the only supported ownership point for sessions created by this server.
    Feature-detect the table and transport method so a future SDK can provide a
    public equivalent without making this lifecycle path fail during startup.
    """
    manager = getattr(server, "session_manager", None)
    instances = getattr(manager, "_server_instances", None)
    if not isinstance(instances, dict):
        return
    for transport in tuple(instances.values()):
        terminate = getattr(transport, "terminate", None)
        if callable(terminate):
            await terminate()


class _RejectServerSentEventsMiddleware:
    """Keep the MCP GET endpoint compliant without opening an unbounded SSE stream.

    DomoAI currently sends a response to every client request and does not send
    unsolicited server-to-client messages. The MCP Streamable HTTP contract
    permits that mode to answer GET with 405. The pinned MCP/sse-starlette
    combination otherwise creates a receive stream for every client GET and
    does not close it on the normal disconnect path. SSE remains an explicit
    deployment opt-in once a dependency version with complete cleanup is
    qualified.
    """

    def __init__(self, app: Any, *, mcp_path: str) -> None:
        self.app = app
        self.mcp_path = mcp_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == self.mcp_path:
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [(b"allow", b"POST, DELETE"), (b"content-length", b"0")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


async def _close_gateway_safely(gateway: GatewayApplication) -> None:
    """Finish gateway cleanup before propagating an interrupt cancellation."""

    close_task = asyncio.create_task(gateway.close(), name="domoai-gateway-close")
    try:
        await asyncio.shield(close_task)
    except asyncio.CancelledError:
        # ``SIGINT`` cancels the task running ``run_gateway``. The close
        # operation must live in its own task so lifecycle cancellation cannot
        # skip ownership release, storage closure, or adapter disconnect.
        await asyncio.shield(close_task)
        raise


async def build_gateway(
    settings: Settings | None = None,
    *,
    operator_principal_provider: OperatorPrincipalProvider | None = None,
    operator_approval_assertion_provider: OperatorApprovalAssertionProvider | None = None,
    require_configured_adapter: bool = True,
) -> GatewayApplication:
    """Build one configured gateway; callers explicitly own start/close."""

    from domoai.mcp.configured import build_configured_server

    runtime, server = await build_configured_server(
        settings,
        operator_principal_provider=operator_principal_provider,
        operator_approval_assertion_provider=operator_approval_assertion_provider,
        require_configured_adapter=require_configured_adapter,
    )
    return GatewayApplication(runtime=runtime, server=server)


async def run_gateway() -> None:
    # The long-lived shared gateway is a deployment entrypoint. It must never
    # silently expose the deterministic simulator when no real provider is
    # configured. The local stdio entrypoint remains the explicit fixture path.
    gateway = await build_gateway()
    await gateway.start()
    try:
        await gateway.server.run_streamable_http_async()
    finally:
        await _close_gateway_safely(gateway)


def main() -> None:
    asyncio.run(run_gateway())
