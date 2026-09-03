import asyncio
import signal
from pathlib import Path

import httpx
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
from domoai.mcp.gateway import (
    GatewayApplication,
    _close_gateway_safely,
    _handle_sigterm,
    create_gateway_server,
    main,
)
from domoai.mcp.ortools_server import OrtoolsMcpContext
from domoai.mcp.unified_server import UnifiedMcpContext
from domoai.optimizer.cp_sat import CpSatOptimizer
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_gateway_uses_one_configured_streamable_http_endpoint(tmp_path: Path) -> None:
    context = await _build_context()
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_port=8123,
        mcp_path="/shared-mcp",
        mcp_public_url="http://127.0.0.1:8123",
    )

    server = create_gateway_server(context, settings)

    assert server.settings.host == "127.0.0.1"
    assert server.settings.port == 8123
    assert server.settings.streamable_http_path == "/shared-mcp"
    assert server.settings.json_response is True
    assert server.settings.max_request_body_size == settings.mcp_max_request_body_size


@pytest.mark.asyncio
async def test_gateway_health_and_readiness_are_sanitized_and_stateful(tmp_path: Path) -> None:
    context = await _build_context()
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_path="/mcp",
        mcp_public_url="http://127.0.0.1:8000",
    )
    runtime = _RuntimeStub(connected=True, settings=settings)
    server = create_gateway_server(context, settings, runtime=runtime)
    gateway = GatewayApplication(runtime=runtime, server=server)

    async with gateway.http_client() as client:
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"service": "domoai", "status": "ok"}

        not_ready = await client.get("/readyz")
        assert not_ready.status_code == 503
        assert not_ready.json()["status"] == "not_ready"
        assert "token" not in not_ready.text.lower()

        await gateway.start()
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert ready.json()["adapter"]["connected"] is True

    await gateway.close()


@pytest.mark.asyncio
async def test_gateway_rejects_missing_and_invalid_network_credentials(tmp_path: Path) -> None:
    token_file = tmp_path / "clients.json"
    token_file.write_text(
        '{"clients": [{"client_id": "codex", "token_hash": "'
        + "0" * 64
        + '" , "scopes": ["read"]}]}',
        encoding="utf-8",
    )
    context = await _build_context()
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="0.0.0.0",
        mcp_public_url="https://gateway.example.test",
        mcp_client_token_file=token_file,
    )
    server = create_gateway_server(context, settings)
    app = server.streamable_http_app()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://gateway.example.test",
    ) as client:
        missing = await client.post("/mcp", json={})
        invalid = await client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer invalid"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401


class _RuntimeStub:
    def __init__(self, *, connected: bool, settings: Settings) -> None:
        self.connected = connected
        self.settings = settings
        self.lifecycle = _LifecycleStub()

    async def health(self):
        from domoai.domain.models import AdapterHealth

        return AdapterHealth(adapter_id="fixture", connected=self.connected)

    async def start(self) -> None:
        await self.lifecycle.start()

    async def close(self) -> None:
        await self.lifecycle.close()


class _LifecycleStub:
    started = False
    closed = False
    running_task_count = 0

    async def start(self) -> None:
        self.started = True
        self.running_task_count = 2

    async def close(self) -> None:
        self.closed = True
        self.running_task_count = 0


@pytest.mark.asyncio
async def test_gateway_close_finishes_before_cancellation_propagates() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()
    main_task = asyncio.current_task()
    assert main_task is not None

    class _SlowGateway:
        async def close(self) -> None:
            started.set()
            await release.wait()
            closed.set()

    async def interrupt_during_close() -> None:
        await started.wait()
        main_task.cancel()
        release.set()

    interrupter = asyncio.create_task(interrupt_during_close())
    try:
        await _close_gateway_safely(_SlowGateway())
    except asyncio.CancelledError:
        pass
    await interrupter

    assert closed.is_set()


def test_gateway_entrypoint_converts_sigterm_into_clean_async_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_handler = signal.getsignal(signal.SIGTERM)
    observed_handlers: list[object] = []

    def fake_run(coroutine: object) -> None:
        observed_handlers.append(signal.getsignal(signal.SIGTERM))
        close = getattr(coroutine, "close", None)
        assert callable(close)
        close()

    monkeypatch.setattr("domoai.mcp.gateway.asyncio.run", fake_run)

    main()

    assert observed_handlers == [_handle_sigterm]
    assert signal.getsignal(signal.SIGTERM) is previous_handler


def test_sigterm_handler_raises_interrupt_for_run_gateway_finally() -> None:
    with pytest.raises(KeyboardInterrupt):
        _handle_sigterm(signal.SIGTERM, None)


async def _build_context() -> UnifiedMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    domotics = DomoticsMcpContext(
        discovery=discovery,
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
    return UnifiedMcpContext(domotics=domotics, optimizer=optimizer)
