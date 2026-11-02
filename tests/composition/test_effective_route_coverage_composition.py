"""Live managed adapter membership reaches the configured MCP resource boundary."""

from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.config.settings import Settings
from domoai.domain.models import AdapterSnapshot
from domoai.mcp.configured import build_configured_server
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from tests.composition.test_scoped_area_inventory_composition import request_authority
from tests.contract.test_effective_route_coverage import read_resource, route_snapshot
from tests.contract.test_scoped_area_inventory_contract import scoped_authority

pytestmark = [pytest.mark.composition, pytest.mark.asyncio]


class CoverageAdapter(SimulatedHomeAdapter):
    def __init__(self, adapter_id: str, household: str = "home") -> None:
        super().__init__(entities=[])
        self.adapter_id = adapter_id
        self.household = household

    async def discover(self) -> AdapterSnapshot:
        return route_snapshot(device_id=f"{self.household}.light", area_id=f"{self.household}.room")


async def test_configured_resources_follow_attach_detach_and_recovery(tmp_path: Path) -> None:
    runtime, server = await build_configured_server(
        Settings(
            database_path=tmp_path / "live.sqlite3",
            connection_master_key_path=tmp_path / "master.key",
        ),
        control_plane_mode=True,
    )
    try:
        managed = runtime.adapter
        assert isinstance(managed, ManagedAdapterSet)
        assert (await read_resource(server, "runtime"))["providers"] == []
        adapter = CoverageAdapter("live-provider")
        await managed.attach(adapter)
        assert (await read_resource(server, "coverage"))["routes"] == []
        await runtime.discovery.refresh()
        active = await read_resource(server, "runtime")
        assert active["providers"] == [{"provider_id": "live-provider", "active": True}]
        assert active["writable_capabilities"][0]["available"] is True
        coverage = await read_resource(server, "coverage")
        assert all(item["status"] == "active" for item in coverage["routes"])
        revision = runtime.state_store.runtime_revision

        await managed.detach(adapter.adapter_id)
        detached = await read_resource(server, "runtime")
        coverage = await read_resource(server, "coverage")
        assert detached["providers"] == []
        assert detached["runtime_revision"] == coverage["runtime_revision"] == revision
        assert detached["writable_capabilities"][0]["available"] is False
        assert all(item["status"] == "unavailable" for item in coverage["routes"])

        await managed.attach(adapter)
        assert (await read_resource(server, "runtime"))["writable_capabilities"][0][
            "available"
        ] is False
        await runtime.discovery.refresh()
        recovered = await read_resource(server, "runtime")
        coverage = await read_resource(server, "coverage")
        assert recovered["providers"] == active["providers"]
        assert recovered["writable_capabilities"] == active["writable_capabilities"]
        assert coverage["runtime_revision"] == recovered["runtime_revision"]
        assert all(item["status"] == "active" for item in coverage["routes"])
        assert adapter.calls == []

        replacement = CoverageAdapter(adapter.adapter_id)
        await managed.replace(adapter.adapter_id, replacement)
        await runtime.discovery.refresh()
        replaced = await read_resource(server, "runtime")
        assert replaced["providers"] == active["providers"]
        assert replaced["writable_capabilities"] == active["writable_capabilities"]
        assert replacement.calls == []
    finally:
        await runtime.close()


async def test_rehydrated_routes_need_live_composition_and_discovery(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "restart.sqlite3",
        connection_master_key_path=tmp_path / "master.key",
    )
    runtime, _ = await build_configured_server(settings, control_plane_mode=True)
    try:
        assert isinstance(runtime.adapter, ManagedAdapterSet)
        await runtime.adapter.attach(CoverageAdapter("live-provider"))
        await runtime.discovery.refresh()
    finally:
        await runtime.close()

    restored, server = await build_configured_server(settings, control_plane_mode=True)
    try:
        assert isinstance(restored.adapter, ManagedAdapterSet)
        assert restored.registry.devices
        assert restored.registry.routes_for("home.light", "brightness") == ()
        before = await read_resource(server, "runtime")
        assert before["providers"] == []
        assert before["writable_capabilities"][0]["available"] is False
        await restored.adapter.attach(CoverageAdapter("live-provider"))
        attached = await read_resource(server, "runtime")
        assert attached["providers"] == [{"provider_id": "live-provider", "active": True}]
        assert attached["writable_capabilities"][0]["available"] is False
        assert all(
            item["status"] == "unavailable"
            for item in (await read_resource(server, "coverage"))["routes"]
        )
        await restored.discovery.refresh()
        discovered = await read_resource(server, "runtime")
        coverage = await read_resource(server, "coverage")
        assert discovered["writable_capabilities"][0]["available"] is True
        assert coverage["runtime_revision"] == discovered["runtime_revision"]
        assert all(item["status"] == "active" for item in coverage["routes"])
    finally:
        await restored.close()


async def test_resource_observation_does_not_call_provider_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, server = await build_configured_server(
        Settings(database_path=tmp_path / "read.sqlite3"), control_plane_mode=True
    )
    try:
        assert isinstance(runtime.adapter, ManagedAdapterSet)
        adapter = CoverageAdapter("live-provider")
        await runtime.adapter.attach(adapter)
        await runtime.discovery.refresh()
        io_calls: list[AsyncMock] = []
        for target, names in (
            (runtime.discovery, ("refresh",)),
            (runtime.adapter, ("discover", "read_state", "health", "execute")),
            (adapter, ("discover", "read_state", "health", "execute")),
        ):
            for name in names:
                spy = AsyncMock(side_effect=AssertionError("resource reads must not call I/O"))
                monkeypatch.setattr(target, name, spy)
                io_calls.append(spy)
        coverage = await read_resource(server, "coverage")
        projected = await read_resource(server, "runtime")
        assert all(item["status"] == "active" for item in coverage["routes"])
        assert projected["writable_capabilities"][0]["available"] is True
        for spy in io_calls:
            spy.assert_not_called()
    finally:
        await runtime.close()


async def test_configured_provider_evidence_is_isolated_between_households(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        homes = []
        for household in ("home-a", "home-b"):
            runtime, server = await build_configured_server(
                Settings(
                    database_path=tmp_path / f"{household}.sqlite3",
                    connection_master_key_path=tmp_path / f"{household}.key",
                    mcp_tenant_id="tenant-a",
                    mcp_household_id=household,
                ),
                control_plane_mode=True,
            )
            stack.push_async_callback(runtime.close)
            assert isinstance(runtime.adapter, ManagedAdapterSet)
            await runtime.adapter.attach(CoverageAdapter(f"{household}-provider", household))
            await runtime.discovery.refresh()
            homes.append((household, server))

        for household, server in homes:
            authority = scoped_authority(
                tenant="tenant-a", household=household, devices=(f"{household}.light",)
            )
            with request_authority(authority):
                projected = await read_resource(server, "runtime")
                coverage = await read_resource(server, "coverage")
            assert projected["providers"] == [
                {"provider_id": f"{household}-provider", "active": True}
            ]
            assert {item["device_id"] for item in coverage["routes"]} == {f"{household}.light"}
            assert projected["writable_capabilities"][0]["available"] is True
            assert coverage["runtime_revision"] == projected["runtime_revision"]
            assert all(item["status"] == "active" for item in coverage["routes"])

            foreign_home = "home-b" if household == "home-a" else "home-a"
            with request_authority(scoped_authority(tenant="tenant-a", household=foreign_home)):
                for resource in ("runtime", "coverage"):
                    denied = await read_resource(server, resource)
                    assert denied["error"]["code"] == "insufficient_scope"
                    assert not {"routes", "providers", "writable_capabilities"}.intersection(denied)
