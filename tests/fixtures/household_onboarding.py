"""One isolated household runtime shared by HTTP, browser and SDK tests."""

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from domoai.config.settings import Settings
from domoai.mcp.gateway import build_gateway


@asynccontextmanager
async def connection_databases(tmp_path: Path, backend: str):
    """Two actual database connections; PostgreSQL is an isolated disposable server."""
    from domoai.persistence.postgres import PostgresDatabase
    from domoai.persistence.sqlite import SQLiteDatabase

    container = None
    if backend == "postgres":
        from testcontainers.community.postgres import PostgresContainer

        container = PostgresContainer(
            "postgres:16-alpine",
            username="domoai",
            password="domoai-test",
            dbname="domoai",
            driver=None,
        )
        container.start()
        first = PostgresDatabase(container.get_connection_url(driver=None))
        second = PostgresDatabase(container.get_connection_url(driver=None))
    else:
        first = SQLiteDatabase(tmp_path / "transfers.sqlite3")
        second = SQLiteDatabase(tmp_path / "transfers.sqlite3")
    try:
        await first.initialize()
        await second.initialize()
        yield first, second
    finally:
        await second.close()
        await first.close()
        if container is not None:
            container.stop()


@asynccontextmanager
async def onboarding_gateway(tmp_path: Path, **overrides):
    settings = Settings(
        database_path=tmp_path / "runtime.sqlite3",
        audit_database_path=tmp_path / "audit.sqlite3",
        commissioning_manifest_path=tmp_path / "commissioning.json",
        connection_master_key_path=tmp_path / "master.key",
        mcp_tenant_id="tenant-a",
        mcp_household_id="home-a",
        mcp_deployment_id="edge",
        mcp_host="127.0.0.1",
        mcp_public_url=overrides.pop("mcp_public_url", "http://127.0.0.1:8765"),
        **overrides,
    )
    gateway = await build_gateway(
        settings, require_configured_adapter=False, control_plane_mode=True
    )
    try:
        await gateway.start()
        yield gateway
    finally:
        await gateway.close()


@dataclass
class HomeAssistantHttpSource:
    """A real local HA HTTP/WebSocket peer, never a physical actuator."""

    token: str = "synthetic-ha-fixture"
    url: str = ""
    reads: int = 0
    commands: int = 0
    states: list[dict] = field(
        default_factory=lambda: [
            {
                "entity_id": "sensor.temperature",
                "state": "21.5",
                "last_updated": datetime.now(UTC).isoformat(),
                "attributes": {
                    "friendly_name": "Salón",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                },
            }
        ]
    )

    async def state_request(self, request):
        self.reads += 1
        if request.headers.get("authorization") != "Bearer " + self.token:
            return JSONResponse({}, status_code=401)
        return JSONResponse(self.states)

    async def command_request(self, _request):
        self.commands += 1
        return JSONResponse([])

    async def websocket(self, websocket):
        await websocket.accept()
        await websocket.send_json({"type": "auth_required", "ha_version": "fixture"})
        auth = await websocket.receive_json()
        if auth.get("access_token") != self.token:
            await websocket.send_json({"type": "auth_invalid"})
            await websocket.close()
            return
        await websocket.send_json({"type": "auth_ok", "ha_version": "fixture"})
        try:
            while True:
                request = await websocket.receive_json()
                await websocket.send_json(
                    {"id": request["id"], "type": "result", "success": True, "result": []}
                )
        except WebSocketDisconnect:
            return


@asynccontextmanager
async def home_assistant_http_source():
    source = HomeAssistantHttpSource()
    app = Starlette(
        routes=[
            Route("/api/states", source.state_request),
            Route("/api/services/{domain}/{service}", source.command_request, methods=["POST"]),
            WebSocketRoute("/api/websocket", source.websocket),
        ]
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        source.url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            for _ in range(100):
                if server.started:
                    break
                if task.done():
                    await task
                await asyncio.sleep(0.02)
            assert server.started, "fixture HTTP server failed to start"
            yield source
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


async def connect_home_assistant(gateway, source):
    async with gateway.http_client() as client:
        created = await client.post(
            "/api/v1/connections",
            json={
                "connector_id": "home_assistant",
                "endpoint": {"url": source.url},
                "credential": source.token,
                "activate": False,
            },
        )
        assert created.status_code == 201, created.text
        profile = created.json()["connection"]
        assert profile["enabled"] is False
        active = await client.post(f"/api/v1/connections/{profile['id']}/activate")
        assert active.status_code == 200, active.text
    return profile


async def restarted_connection_probe(database, backend: str, root: Path) -> dict:
    """A fresh interpreter opens only this fixture DB/store; no adapter is activated."""
    import json
    import subprocess
    import sys

    address = str(database.path) if backend == "sqlite" else database.dsn
    script = """
import asyncio, json, sys
from pathlib import Path
from domoai.application.connection_manager import ConnectionManager
from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.persistence.postgres import PostgresDatabase
from domoai.security.secrets import FileSecretStore
from tests.integration.test_connection_manager import _build_manager
async def main():
    data = json.load(sys.stdin)
    root = Path(data["root"])
    prototype, prototype_db, _, store = await _build_manager(root)
    await prototype_db.close()
    db = (SQLiteDatabase(Path(data["address"])) if data["backend"] == "sqlite"
          else PostgresDatabase(data["address"]))
    await db.initialize()
    manager = ConnectionManager(repository=ConnectionRepository(db, scope=LeaseScope(
        tenant_id="default", household_id="default", deployment_id="default")),
        providers=prototype.providers, runtime=prototype.runtime, secret_store=store)
    try:
        diagnostics = await manager.recover()
        profiles = await manager.list()
        print(json.dumps({"diagnostics": [item.model_dump(mode="json") for item in diagnostics],
                          "profiles": [item.model_dump(mode="json") for item in profiles],
                          "children": len(manager.runtime.adapters),
                          "owned_transfers": len(manager._owned_transfers)}))
    finally:
        await db.close()
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script],
        input=json.dumps({"backend": backend, "address": address, "root": str(root)}),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)
