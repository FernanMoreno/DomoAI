import asyncio
import hashlib
import json
import platform
import socket
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import TokenVerifier
from mcp.types import Implementation
from pydantic import AnyUrl
from sse_starlette.sse import AppStatus

from domoai.config.settings import Settings
from domoai.mcp.auth import StaticBearerTokenVerifier
from domoai.mcp.gateway import build_gateway
from domoai.mcp.probe import (
    ClientSessionEvidence,
    MCPClientProbe,
    ProbeFailure,
)
from domoai.mcp.qualification import (
    ClientQualificationEvidence,
    QualificationStage,
    qualification_configuration_digest,
    source_snapshot_digest,
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


@asynccontextmanager
async def _observe_qualification_context[T](
    context: AbstractAsyncContextManager[T],
    name: str,
    outcomes: list[tuple[str, bool]],
) -> AsyncIterator[T]:
    """Keep body failure separate from each real context's exit outcome."""
    entered = False
    body_error: Exception | asyncio.CancelledError | None = None
    try:
        async with context as resource:
            entered = True
            try:
                yield resource
            except (Exception, asyncio.CancelledError) as error:
                # Retain the failure before SDK/task-group exit can replace it.
                # Re-raise after cleanup, including cancellation of this task.
                body_error = error
    except (Exception, asyncio.CancelledError) as cleanup_error:
        if entered:
            outcomes.append((name, False))
        if body_error is None or isinstance(cleanup_error, asyncio.CancelledError):
            raise
        # Cancellation takes precedence over a simultaneous cleanup failure.
        # An ordinary body failure also retains its original failed stage.
    else:
        outcomes.append((name, True))
    if body_error is not None:
        raise body_error


async def _run_test_owned_qualification(
    *,
    gateway: Any,
    token: str,
    token_verifier: TokenVerifier | None,
    server_revision: str,
) -> ClientQualificationEvidence:
    """Execute P10 only against the gateway that this test created and started."""

    settings = gateway.runtime.settings
    endpoint = settings.mcp_public_url + settings.mcp_path
    source_root = Path(__file__).resolve().parents[2] / "src" / "domoai" / "mcp"
    client_sources = (
        source_root / "qualification.py",
        source_root / "probe.py",
        Path(__file__).resolve(),
    )
    server_sources = (
        source_root / "gateway.py",
        source_root / "configured.py",
        source_root / "unified_server.py",
        source_root / "auth.py",
        source_root / "domotics_server.py",
        source_root / "resources.py",
    )
    client_source_before = source_snapshot_digest(client_sources)
    server_source_before = source_snapshot_digest(server_sources)
    stages: list[QualificationStage] = []
    protocol_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    runtime_revision: str | None = None
    attempted_stage: str | None = None
    cleanup_completed = False
    cleanup_failed = False
    try:
        if token_verifier is None:
            raise ValueError("test-owned qualification needs a token verifier")
        verified = await token_verifier.verify_token(token)
        if verified is None or verified.scopes != ["read"]:
            raise ValueError("test-owned qualification needs a verified read-only token")
        cleanup_outcomes: list[tuple[str, bool]] = []
        try:
            async with _observe_qualification_context(
                httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}),
                "http-client",
                cleanup_outcomes,
            ) as client:
                async with _observe_qualification_context(
                    streamable_http_client(endpoint, http_client=client),
                    "first-stream",
                    cleanup_outcomes,
                ) as (read_stream, write_stream, _):
                    async with _observe_qualification_context(
                        ClientSession(
                            read_stream,
                            write_stream,
                            client_info=Implementation(
                                name="generic-sdk-local", version=version("mcp")
                            ),
                        ),
                        "first-session",
                        cleanup_outcomes,
                    ) as session:
                        attempted_stage = "initialize"
                        initialized = await session.initialize()
                        protocol_version = str(initialized.protocolVersion)
                        server_name = initialized.serverInfo.name
                        server_version = initialized.serverInfo.version
                        stages.extend(
                            (
                                QualificationStage("connect", "passed", "sdk-connect"),
                                QualificationStage("initialize", "passed", "sdk-initialize"),
                            )
                        )
                        attempted_stage = "catalog"
                        tools = await session.list_tools()
                        resources = await session.list_resources()
                        assert tools.tools and resources.resources
                        stages.append(QualificationStage("catalog", "passed", "sdk-catalog"))
                        attempted_stage = "runtime_read"
                        runtime = await session.read_resource(cast(AnyUrl, "domotics://runtime"))
                        text = getattr(runtime.contents[0], "text", None)
                        assert isinstance(text, str)
                        payload = json.loads(text)
                        assert isinstance(payload, dict)
                        runtime_revision = payload["runtime_revision"]
                        assert isinstance(runtime_revision, str)
                        stages.append(
                            QualificationStage("runtime_read", "passed", "sdk-runtime-read")
                        )
                        attempted_stage = "scope_denial"
                        denial = await session.call_tool(
                            "execute_plan",
                            {
                                "plan_id": "p10-read-only",
                                "validation_digest": "p10-read-only",
                            },
                        )
                        assert denial.isError is True
                        assert denial.structuredContent is not None
                        assert denial.structuredContent["error"]["code"] == "insufficient_scope"
                        stages.append(
                            QualificationStage(
                                "scope_denial",
                                "passed",
                                "sdk-scope-denial",
                                expected_error_code="insufficient_scope",
                            )
                        )
                async with _observe_qualification_context(
                    streamable_http_client(endpoint, http_client=client),
                    "second-stream",
                    cleanup_outcomes,
                ) as (read_stream, write_stream, _):
                    async with _observe_qualification_context(
                        ClientSession(
                            read_stream,
                            write_stream,
                            client_info=Implementation(
                                name="generic-sdk-local", version=version("mcp")
                            ),
                        ),
                        "second-session",
                        cleanup_outcomes,
                    ) as session:
                        attempted_stage = "reconnect"
                        await session.initialize()
                        await session.list_tools()
                        verified_after_reconnect = await token_verifier.verify_token(token)
                        assert verified_after_reconnect is not None
                        assert verified_after_reconnect.scopes == ["read"]
                        stages.append(QualificationStage("reconnect", "passed", "sdk-reconnect"))
        finally:
            cleanup_failed = any(not passed for _, passed in cleanup_outcomes)
            cleanup_completed = bool(cleanup_outcomes) and not cleanup_failed
        stages.append(QualificationStage("clean_shutdown", "passed", "sdk-clean-shutdown"))
        status: Literal["minimum-qualified", "failed"] = "minimum-qualified"
    except Exception:
        completed = {stage.name for stage in stages}
        if attempted_stage is not None and attempted_stage not in completed:
            stages.append(QualificationStage(cast(Any, attempted_stage), "failed", "sdk-failure"))
            completed.add(cast(Any, attempted_stage))
        for name in (
            "connect",
            "initialize",
            "catalog",
            "runtime_read",
            "scope_denial",
            "reconnect",
            "clean_shutdown",
        ):
            if name not in completed:
                cleanup_status: Literal["passed", "failed", "blocked"] = (
                    "failed"
                    if cleanup_failed and name == "clean_shutdown"
                    else "passed"
                    if cleanup_completed and name == "clean_shutdown"
                    else "blocked"
                )
                stages.append(
                    QualificationStage(
                        cast(Any, name),
                        cleanup_status if name == "clean_shutdown" else "blocked",
                        f"sdk-{name}",
                    )
                )
        status = "failed"

    client_source_after = source_snapshot_digest(client_sources)
    server_source_after = source_snapshot_digest(server_sources)
    provenance_changed = status == "minimum-qualified" and (
        client_source_before != client_source_after or server_source_before != server_source_after
    )
    if provenance_changed:
        status = "failed"
    return ClientQualificationEvidence(
        qualification_id=f"p10-{uuid4().hex}",
        client_name="generic-sdk-local",
        client_version=version("mcp"),
        client_build=None,
        system={
            "os": platform.system(),
            "os_version": platform.release(),
            "python": platform.python_version(),
        },
        transport="streamable-http",
        auth_mode="incoming-static-bearer-a05",
        negotiated_protocol_version=protocol_version,
        server_name=server_name,
        server_version=server_version,
        server_revision=server_revision,
        client_source_snapshot_digest=client_source_after,
        server_source_snapshot_digest=server_source_after,
        client_source_snapshot_scope=(
            "src/domoai/mcp/qualification.py",
            "src/domoai/mcp/probe.py",
            "tests/integration/test_mcp_gateway_http.py",
        ),
        server_source_snapshot_scope=(
            "src/domoai/mcp/gateway.py",
            "src/domoai/mcp/configured.py",
            "src/domoai/mcp/unified_server.py",
            "src/domoai/mcp/auth.py",
            "src/domoai/mcp/domotics_server.py",
            "src/domoai/mcp/resources.py",
        ),
        configuration_digest=qualification_configuration_digest(
            {
                "auth_mode": "incoming-static-bearer-a05",
                "json_response": settings.mcp_json_response,
                "server_sent_events": settings.mcp_server_sent_events,
                "transport": "streamable-http",
            }
        ),
        tested_at=datetime.now(UTC).isoformat(),
        runtime_revision=runtime_revision,
        scenarios=tuple(stages),
        status=status,
        limitations=(
            "local-sdk-only",
            "no-named-agent-ui",
            *(("source-snapshot-changed",) if provenance_changed else ()),
        ),
    )


@pytest.mark.asyncio
async def test_local_sdk_qualification_records_actual_read_and_scope_denial_stages(
    tmp_path: Path,
) -> None:
    """P10: only a test-owned loopback gateway may exercise scope denial."""

    port = _free_port()
    token_path = tmp_path / "clients.json"
    tokens = _write_multi_agent_tokens(token_path)
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=token_path,
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        evidence = await _run_test_owned_qualification(
            gateway=gateway,
            server_revision="p10-test-build",
            token=tokens["codex"],
            token_verifier=StaticBearerTokenVerifier.from_file(token_path),
        )

        assert evidence.status == "minimum-qualified"
        assert evidence.client_name == "generic-sdk-local"
        assert evidence.server_revision == "p10-test-build"
        assert evidence.runtime_revision
        assert evidence.negotiated_protocol_version
        assert {stage.name for stage in evidence.scenarios} == {
            "connect",
            "initialize",
            "catalog",
            "runtime_read",
            "scope_denial",
            "reconnect",
            "clean_shutdown",
        }
        assert all(stage.status == "passed" for stage in evidence.scenarios)
        scope_denial = next(stage for stage in evidence.scenarios if stage.name == "scope_denial")
        assert scope_denial.expected_error_code == "insufficient_scope"
        assert getattr(gateway.runtime.adapter, "calls", []) == []

        serialized = json.dumps(evidence.as_dict(), sort_keys=True)
        artifact = tmp_path / "p10-generic-sdk-local-evidence.json"
        artifact.write_text(serialized + "\n", encoding="utf-8")
        assert json.loads(artifact.read_text(encoding="utf-8")) == json.loads(serialized)
        assert tokens["codex"] not in serialized
        assert hashlib.sha256(tokens["codex"].encode()).hexdigest() not in serialized
        assert str(token_path) not in serialized
        assert settings.mcp_public_url not in serialized
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


@pytest.mark.asyncio
async def test_local_sdk_qualification_cannot_qualify_an_unverified_token(tmp_path: Path) -> None:
    port = _free_port()
    token_path = tmp_path / "clients.json"
    _write_multi_agent_tokens(token_path)
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=token_path,
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    try:
        await _wait_for_server(server)
        evidence = await _run_test_owned_qualification(
            gateway=gateway,
            server_revision="p10-test-build",
            token="not-a-test-token",
            token_verifier=StaticBearerTokenVerifier.from_file(token_path),
        )

        assert evidence.status == "failed"
        assert all(stage.status == "blocked" for stage in evidence.scenarios)
        assert getattr(gateway.runtime.adapter, "calls", []) == []
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


def _inject_qualification_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    layer: str,
    token: str,
    target_exit: int,
    observed: list[str],
    error_type: type[Exception] | type[asyncio.CancelledError] = RuntimeError,
) -> None:
    """Fault the selected real exit, after its resource cleanup has run."""
    exits = 0
    if layer == "stream":
        original_stream = streamable_http_client

        @asynccontextmanager
        async def failing_stream(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            nonlocal exits
            try:
                async with original_stream(*args, **kwargs) as streams:
                    yield streams
            finally:
                exits += 1
                if exits == target_exit:
                    observed.append(f"stream-exit-{exits}")
                    raise error_type("review-stream-cleanup-failure")

        monkeypatch.setattr(
            "tests.integration.test_mcp_gateway_http.streamable_http_client", failing_stream
        )
        return

    context_type = ClientSession if layer == "session" else httpx.AsyncClient
    original_exit = context_type.__aexit__

    async def failing_exit(
        context: Any,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        nonlocal exits
        try:
            return await original_exit(context, exc_type, exc_value, traceback)
        finally:
            selected = (
                layer == "session" or context.headers.get("Authorization") == f"Bearer {token}"
            )
            if selected:
                exits += 1
                if layer == "http" or exits == target_exit:
                    observed.append(f"{layer}-exit-{exits}")
                    raise error_type(f"review-{layer}-cleanup-failure")

    monkeypatch.setattr(context_type, "__aexit__", failing_exit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "initialize",
        "reconnect",
        "cleanup",
        "initialize_cleanup",
        "source_change",
        "initialize_session_cleanup",
        "reconnect_session_cleanup",
        "session_cleanup",
        "stream_cleanup",
        "initialize_stream_cleanup",
        "reconnect_stream_cleanup",
        "reconnect_cleanup",
    ],
)
async def test_local_sdk_qualification_withholds_qualification_on_observed_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    port = _free_port()
    token_path = tmp_path / "clients.json"
    tokens = _write_multi_agent_tokens(token_path)
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=token_path,
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    if failure.startswith(("initialize", "reconnect")):
        original_initialize = ClientSession.initialize
        calls = 0

        async def failing_initialize(session: ClientSession) -> Any:
            nonlocal calls
            calls += 1
            if failure.startswith("initialize") or calls == 2:
                raise RuntimeError(f"forced-{failure}-failure")
            return await original_initialize(session)

        monkeypatch.setattr(ClientSession, "initialize", failing_initialize)
    observed_cleanup_failures: list[str] = []
    if "cleanup" in failure:
        layer = "session" if "session" in failure else "stream" if "stream" in failure else "http"
        _inject_qualification_cleanup_failure(
            monkeypatch,
            layer=layer,
            token=tokens["codex"],
            target_exit=2 if failure.startswith("reconnect") else 1,
            observed=observed_cleanup_failures,
        )
    elif failure == "source_change":
        digests = iter(
            (
                "sha256:" + "a" * 64,
                "sha256:" + "b" * 64,
                "sha256:" + "c" * 64,
                "sha256:" + "d" * 64,
            )
        )
        monkeypatch.setattr(
            "tests.integration.test_mcp_gateway_http.source_snapshot_digest",
            lambda _: next(digests),
        )

    try:
        await _wait_for_server(server)
        evidence = await _run_test_owned_qualification(
            gateway=gateway,
            server_revision="p10-test-build",
            token=tokens["codex"],
            token_verifier=StaticBearerTokenVerifier.from_file(token_path),
        )

        if "cleanup" in failure:
            assert observed_cleanup_failures == [
                f"{layer}-exit-{2 if failure.startswith('reconnect') and layer != 'http' else 1}"
            ]
            print(
                f"{failure}: actual {layer} exit raised; stages="
                f"{[(stage.name, stage.status) for stage in evidence.scenarios]}"
            )
        assert evidence.status == "failed"
        clean_shutdown = next(
            stage for stage in evidence.scenarios if stage.name == "clean_shutdown"
        )
        if failure in {"initialize", "reconnect", "source_change"}:
            assert clean_shutdown.status == "passed"
        else:
            assert clean_shutdown.status == "failed"
        statuses = {stage.name: stage.status for stage in evidence.scenarios}
        if failure.startswith("initialize"):
            assert statuses["initialize"] == "failed"
            for stage in ("catalog", "runtime_read", "scope_denial", "reconnect"):
                assert statuses[stage] == "blocked"
        elif failure.startswith("reconnect"):
            assert statuses["scope_denial"] == "passed"
            assert statuses["reconnect"] == "failed"
        elif failure in {"session_cleanup", "stream_cleanup"}:
            assert statuses["scope_denial"] == "passed"
            assert statuses["reconnect"] == "blocked"
        if failure == "source_change":
            assert "source-snapshot-changed" in evidence.limitations
            assert all(stage.status == "passed" for stage in evidence.scenarios)
        assert getattr(gateway.runtime.adapter, "calls", []) == []
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancellation_source", "cleanup_layer"),
    [
        ("body", "none"),
        ("body", "session"),
        ("body", "stream"),
        ("body", "http"),
        ("exit_with_body_failure", "session"),
        ("exit_with_body_failure", "stream"),
        ("exit_with_body_failure", "http"),
    ],
)
@pytest.mark.parametrize("cancel_at", [1, 2], ids=["initialize", "reconnect"])
async def test_local_sdk_qualification_propagates_cancellation_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_layer: str,
    cancellation_source: str,
    cancel_at: int,
) -> None:
    port = _free_port()
    token_path = tmp_path / "clients.json"
    tokens = _write_multi_agent_tokens(token_path)
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=token_path,
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())

    original_initialize = ClientSession.initialize
    calls = 0

    async def cancelled_initialize(session: ClientSession) -> Any:
        nonlocal calls
        calls += 1
        if calls == cancel_at:
            if cancellation_source == "body":
                raise asyncio.CancelledError
            raise RuntimeError("review-body-failure-before-exit-cancellation")
        return await original_initialize(session)

    observed_cleanup_failures: list[str] = []
    if cleanup_layer != "none":
        _inject_qualification_cleanup_failure(
            monkeypatch,
            layer=cleanup_layer,
            token=tokens["codex"],
            target_exit=cancel_at,
            observed=observed_cleanup_failures,
            error_type=(
                asyncio.CancelledError
                if cancellation_source == "exit_with_body_failure"
                else RuntimeError
            ),
        )
    monkeypatch.setattr(ClientSession, "initialize", cancelled_initialize)
    try:
        await _wait_for_server(server)
        with pytest.raises(asyncio.CancelledError):
            await _run_test_owned_qualification(
                gateway=gateway,
                server_revision="p10-test-build",
                token=tokens["codex"],
                token_verifier=StaticBearerTokenVerifier.from_file(token_path),
            )
        if cleanup_layer != "none":
            assert observed_cleanup_failures == [
                f"{cleanup_layer}-exit-{1 if cleanup_layer == 'http' else cancel_at}"
            ]
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
