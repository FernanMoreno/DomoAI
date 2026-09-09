from __future__ import annotations

from pathlib import Path

import pytest

from domoai.application.runtime_factory import create_adapter
from domoai.config.settings import Settings
from domoai.domain.models import Command
from domoai.mcp.gateway import build_gateway
from domoai.runtime.managed_adapter_set import ManagedAdapterSet


def test_control_plane_factory_selects_empty_managed_runtime(tmp_path: Path) -> None:
    adapter = create_adapter(
        Settings(database_path=tmp_path / "runtime.sqlite3"),
        control_plane_mode=True,
    )

    assert isinstance(adapter, ManagedAdapterSet)
    assert adapter.adapters == ()


@pytest.mark.asyncio
async def test_control_plane_gateway_boots_without_physical_provider(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "runtime.sqlite3",
        connection_master_key_path=tmp_path / "master.key",
    )
    gateway = await build_gateway(
        settings,
        require_configured_adapter=False,
        control_plane_mode=True,
    )
    try:
        assert isinstance(gateway.runtime.adapter, ManagedAdapterSet)
        assert gateway.runtime.adapter.adapters == ()
        health = await gateway.runtime.adapter.health()
        assert health.components == []
        assert gateway.runtime.registry.devices == []
        async with gateway.http_client() as client:
            response = await client.get("/")
            connections = await client.get("/api/v1/connections")
            ready = await client.get("/readyz")
            legacy = await client.post("/api/v1/legacy/import", json={})
        rejected = await gateway.runtime.adapter.execute(
            Command(
                id="zero-provider-command",
                device_id="physical-device",
                command="turn_on",
                idempotency_key="zero-provider-command",
            )
        )
        assert response.status_code == 200
        assert "First install" in response.text
        assert connections.status_code == 200
        assert connections.json() == {"connections": []}
        assert ready.status_code == 503
        assert ready.json()["physical_sources"] == 0
        assert legacy.status_code == 200
        assert legacy.json()["profiles"] == []
        assert rejected.accepted is False
        assert "unavailable" in (rejected.message or "").casefold()
    finally:
        await gateway.close()


@pytest.mark.asyncio
async def test_control_plane_gateway_wires_mqtt_discovery_source(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "runtime.sqlite3",
        connection_master_key_path=tmp_path / "master.key",
        generic_mqtt_discovery_url="mqtt://broker.test:1883",
    )
    gateway = await build_gateway(
        settings,
        require_configured_adapter=False,
        control_plane_mode=True,
    )
    try:
        assert gateway.runtime.mqtt_discovery is not None
        management_mount = next(route for route in gateway.app.routes if route.path == "")
        paths = {route.path for route in management_mount.app.routes if hasattr(route, "path")}
        assert "/api/v1/mqtt/discovery/preview" in paths
        assert "/api/v1/mqtt/discovery/persist" in paths
    finally:
        await gateway.close()
