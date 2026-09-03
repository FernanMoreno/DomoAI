import asyncio
import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sse_starlette.sse import AppStatus

from domoai.config.settings import Settings
from domoai.mcp.gateway import build_gateway
from domoai.mcp.probe import (
    ClientSessionEvidence,
    MCPClientProbe,
    ProbeFailure,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _wait_for_server(server: uvicorn.Server) -> None:
    for _ in range(100):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("uvicorn did not start the MCP gateway")


async def _wait_for_sse_shutdown_watchers() -> None:
    """Give the dependency watcher a bounded turn to observe shutdown.

    sse-starlette owns the watcher task and does not expose its handle. The
    project can still own the lifecycle boundary by keeping AppStatus.should_exit
    asserted until the task has observed it, then checking that no categorized
    watcher remains before resetting the process-global flag.
    """
    for _ in range(40):
        watchers = [
            task
            for task in asyncio.all_tasks()
            if not task.done()
            and "_shutdown_watcher" in getattr(task.get_coro(), "__qualname__", "")
        ]
        if not watchers:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("sse-starlette shutdown watcher did not drain")


async def _shutdown_gateway_server(
    server: uvicorn.Server, server_task: "asyncio.Task[None]", gateway: Any
) -> None:
    """Stop a test-owned uvicorn server without hanging on sse_starlette.

    Setting ``server.should_exit`` here is a programmatic poke, not a real
    OS signal, so it never reaches sse_starlette's ``AppStatus.handle_exit``
    monkeypatch (only an actual SIGTERM/SIGINT delivery does). sse_starlette
    then falls back to polling ``uvicorn_server.should_exit`` via fragile
    signal-handler introspection, which can silently fail to observe this
    server instance and leave any open SSE connection's shutdown-listener
    task waiting forever. Flipping ``AppStatus.should_exit`` directly is the
    same global flag sse_starlette's watcher checks unconditionally first,
    so this reaches it reliably regardless of that introspection outcome.
    Reset it afterward -- it is process-global, and leaving it ``True``
    would make every SSE connection in a later test in this process exit
    immediately instead of running normally.
    """
    await gateway.close_http_sessions()
    AppStatus.should_exit = True
    try:
        # Set the transport signal first and give the SSE response one event
        # loop turn to close before Uvicorn begins connection shutdown. If
        # both flags are set in the same turn, Uvicorn can finish its serve
        # task while the response task still owns memory streams.
        await asyncio.sleep(0.6)
        server.should_exit = True
        await server_task
        await _wait_for_sse_shutdown_watchers()
    finally:
        AppStatus.should_exit = False
    await gateway.close()


@pytest.mark.asyncio
async def test_any_mcp_client_can_use_the_shared_network_catalog(tmp_path: Path) -> None:
    port = _free_port()
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        async with streamable_http_client(settings.mcp_public_url + settings.mcp_path) as (
            read_stream,
            write_stream,
            _,
        ):
            try:
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} >= {
                        "discover_devices",
                        "validate_plan",
                        "execute_plan",
                        "optimize_scenario",
                    }
            finally:
                await read_stream.aclose()
                await write_stream.aclose()
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)

async def _list_catalog_and_state(endpoint: str) -> tuple[set[str], dict[str, object]]:
    async with streamable_http_client(endpoint) as (read_stream, write_stream, _):
        try:
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                discovery = await session.call_tool("discover_devices", {"refresh": False})
                return {tool.name for tool in tools.tools}, discovery.structuredContent or {}
        finally:
            await read_stream.aclose()
            await write_stream.aclose()


@pytest.mark.asyncio
async def test_multiple_network_clients_share_catalog_and_state(tmp_path: Path) -> None:
    port = _free_port()
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        endpoint = settings.mcp_public_url + settings.mcp_path
        clients = await asyncio.gather(
            *(_list_catalog_and_state(endpoint) for _ in range(4))
        )
        first = clients[0]
        assert all(client[0] == first[0] for client in clients)
        assert all(client[1]["devices"] == first[1]["devices"] for client in clients)
        assert all(
            client[1]["runtime_revision"] == first[1]["runtime_revision"]
            for client in clients
        )
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


def _write_multi_agent_tokens(path: Path) -> dict[str, str]:
    tokens = {
        "codex": "codex-test-secret",
        "claude": "claude-test-secret",
        "opencode": "opencode-test-secret",
        "gemini": "gemini-test-secret",
    }
    path.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": client_id,
                        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                        "scopes": ["read"],
                    }
                    for client_id, token in tokens.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    return tokens


async def _run_probe(endpoint: str, client_id: str, token: str) -> ClientSessionEvidence:
    return await MCPClientProbe(
        endpoint=endpoint,
        token=token,
        client_label=client_id,
    ).run()


async def _call_tool(
    endpoint: str,
    token: str,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as client:
        async with streamable_http_client(endpoint, http_client=client) as (
            read_stream,
            write_stream,
            _,
        ):
            try:
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
                    assert result.structuredContent is not None
                    # Let the SDK's POST task finish its response context
                    # before this short-lived test client closes its streams.
                    await asyncio.sleep(0)
                    return result.structuredContent
            finally:
                await read_stream.aclose()
                await write_stream.aclose()


@pytest.mark.asyncio
async def test_four_authenticated_agents_share_one_canonical_gateway(
    tmp_path: Path,
) -> None:
    port = _free_port()
    tokens = _write_multi_agent_tokens(tmp_path / "clients.json")
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=tmp_path / "clients.json",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        endpoint = settings.mcp_public_url + settings.mcp_path
        evidence = await asyncio.gather(
            *(_run_probe(endpoint, client_id, token) for client_id, token in tokens.items())
        )

        assert {item.client_label for item in evidence} == set(tokens)
        assert {item.catalog_digest for item in evidence} == {evidence[0].catalog_digest}
        assert {item.runtime_revision for item in evidence} == {evidence[0].runtime_revision}
        assert {item.registry_digest for item in evidence} == {evidence[0].registry_digest}
        assert {item.discovery_digest for item in evidence} == {evidence[0].discovery_digest}
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


@pytest.mark.asyncio
async def test_read_only_client_scope_and_identity_are_request_local(tmp_path: Path) -> None:
    port = _free_port()
    tokens = _write_multi_agent_tokens(tmp_path / "clients.json")
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=tmp_path / "clients.json",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        endpoint = settings.mcp_public_url + settings.mcp_path
        scope_results = [
            await _call_tool(
                endpoint,
                token,
                "execute_plan",
                {"plan_id": "not-authorized", "validation_digest": "invalid"},
            )
            for token in tokens.values()
        ]
        error_codes = set()
        for result in scope_results:
            error = result.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                error_codes.add(error["code"])
        assert error_codes == {"insufficient_scope"}
        assert {
            event.actor
            for event in gateway.runtime.audit.events
            if event.event_type == "mcp_authorization_rejected"
        } >= {f"agent:{client_id}" for client_id in tokens}
        assert getattr(gateway.runtime.adapter, "calls", []) == []
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


@pytest.mark.asyncio
async def test_invalid_authenticated_probe_does_not_dispatch_a_domain_tool(
    tmp_path: Path,
) -> None:
    port = _free_port()
    _write_multi_agent_tokens(tmp_path / "clients.json")
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=tmp_path / "clients.json",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        calls_before = len(getattr(gateway.runtime.adapter, "calls", []))
        with pytest.raises(ProbeFailure) as error:
            await _run_probe(
                settings.mcp_public_url + settings.mcp_path,
                "invalid",
                "not-a-valid-token",
            )
        assert error.value.category == "authentication"
        assert len(getattr(gateway.runtime.adapter, "calls", [])) == calls_before
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


@pytest.mark.asyncio
async def test_gateway_shutdown_drains_transport_watcher_before_global_reset(
    tmp_path: Path,
) -> None:
    port = _free_port()
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        endpoint = settings.mcp_public_url + settings.mcp_path
        await _run_probe(endpoint, "lifecycle", "probe")
        async with httpx.AsyncClient(base_url=settings.mcp_public_url) as client:
            response = await client.get(settings.mcp_path)
        assert response.status_code == 405
        assert response.headers["allow"] == "POST, DELETE"
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)

    second_gateway = await build_gateway(settings, require_configured_adapter=False)
    await second_gateway.start()
    second_server = uvicorn.Server(
        uvicorn.Config(
            second_gateway.app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    second_server_task = asyncio.create_task(second_server.serve())
    try:
        await _wait_for_server(second_server)
        await _run_probe(endpoint, "lifecycle-reconnect", "probe")
    finally:
        await _shutdown_gateway_server(second_server, second_server_task, second_gateway)


@pytest.mark.asyncio
async def test_one_client_cancellation_does_not_close_shared_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _free_port()
    tokens = _write_multi_agent_tokens(tmp_path / "clients.json")
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=tmp_path / "clients.json",
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    entered = asyncio.Event()
    release = asyncio.Event()
    original_refresh = gateway.runtime.discovery.refresh

    async def blocked_refresh() -> Any:
        entered.set()
        await release.wait()
        return await original_refresh()

    monkeypatch.setattr(gateway.runtime.discovery, "refresh", blocked_refresh)
    endpoint = settings.mcp_public_url + settings.mcp_path
    try:
        await _wait_for_server(server)
        cancelled = asyncio.create_task(
            _call_tool(endpoint, tokens["codex"], "discover_devices", {"refresh": True})
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        cancelled.cancel()
        try:
            await cancelled
        except asyncio.CancelledError:
            pass
        except BaseExceptionGroup as error:
            # MCP 1.29 wraps the cancelled POST task's disconnect/500 race in
            # an AnyIO ExceptionGroup. It is still the expected cancellation
            # path; do not turn it into a gateway failure.
            assert "HTTPStatusError" in repr(error)
        monkeypatch.setattr(gateway.runtime.discovery, "refresh", original_refresh)
        survivor = await _run_probe(endpoint, "claude", tokens["claude"])
        reconnected = await _run_probe(endpoint, "codex", tokens["codex"])
        assert survivor.runtime_revision == reconnected.runtime_revision
        assert gateway.runtime.lifecycle.started is True
    finally:
        release.set()
        monkeypatch.setattr(gateway.runtime.discovery, "refresh", original_refresh)
        await _shutdown_gateway_server(server, server_task, gateway)
