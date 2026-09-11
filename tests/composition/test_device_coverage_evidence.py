"""Configured MCP coverage reads compose with live membership and isolated SQLite."""

import json
from contextlib import AsyncExitStack
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from domoai.adapters.mqtt.recipe_adapter import MqttRecipeTransport
from domoai.application.connection_providers import ConnectionProviderManifest, ConnectionSetup
from domoai.application.connector_qualification import ConnectorQualificationService
from domoai.application.connector_recipes import RecipeConnectionProvider
from domoai.config.settings import Settings
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.mcp.configured import build_configured_server
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.mqtt_transport import InMemoryMqttTransport, MqttMessage
from tests.composition.test_connector_recipe_composition import _ManualDiscovery, _recipe
from tests.composition.test_effective_route_coverage_composition import CoverageAdapter
from tests.composition.test_scoped_area_inventory_composition import request_authority
from tests.contract.test_effective_route_coverage import read_resource
from tests.contract.test_scoped_area_inventory_contract import scoped_authority
from tests.fixtures.device_coverage import RESOURCE, assert_unknown

pytestmark = [pytest.mark.composition, pytest.mark.asyncio]


async def test_configured_evidence_tracks_detachment_and_restart(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "home.sqlite3")
    runtime, server = await build_configured_server(settings, control_plane_mode=True)
    try:
        empty = await read_resource(server, RESOURCE)
        assert empty["rows"] == []
        assert len(empty["families"]) == 5
        assert all(
            "not_present_in_visible_inventory" in f["missing_requirements"]
            for f in empty["families"]
        )
        assert isinstance(runtime.adapter, ManagedAdapterSet)
        adapter = CoverageAdapter("live")
        await runtime.adapter.attach(adapter)
        await runtime.discovery.refresh()
        before = await read_resource(server, RESOURCE)
        assert all(row["status"] == "active" for row in before["rows"])
        await runtime.adapter.detach("live")
        detached = await read_resource(server, RESOURCE)
        assert detached["runtime_revision"] == before["runtime_revision"]
        assert all(row["status"] == "unavailable" for row in detached["rows"])
        await runtime.adapter.attach(adapter)
        await runtime.discovery.refresh()
    finally:
        await runtime.close()

    restored, server = await build_configured_server(settings, control_plane_mode=True)
    try:
        assert isinstance(restored.adapter, ManagedAdapterSet)
        rehydrated = await read_resource(server, RESOURCE)
        assert rehydrated["rows"]
        assert all(row["reasons"] == ["route_missing"] for row in rehydrated["rows"])
        for row in rehydrated["rows"]:
            assert_unknown(row)
        await restored.adapter.attach(CoverageAdapter("live"))
        attached = await read_resource(server, RESOURCE)
        assert all(row["status"] == "unavailable" for row in attached["rows"])
        await restored.discovery.refresh()
        recovered = await read_resource(server, RESOURCE)
        assert all(row["status"] == "active" for row in recovered["rows"])
        for row in recovered["rows"]:
            assert_unknown(row)
    finally:
        await restored.close()


async def test_configured_two_homes_do_not_leak_rows_routes_or_families(tmp_path: Path) -> None:
    async with AsyncExitStack() as stack:
        for home in ("home-a", "home-b"):
            runtime, server = await build_configured_server(
                Settings(
                    database_path=tmp_path / f"{home}.sqlite3",
                    mcp_tenant_id="tenant-a",
                    mcp_household_id=home,
                ),
                control_plane_mode=True,
            )
            stack.push_async_callback(runtime.close)
            assert isinstance(runtime.adapter, ManagedAdapterSet)
            await runtime.adapter.attach(CoverageAdapter(f"{home}-provider", home))
            await runtime.discovery.refresh()
            with request_authority(
                scoped_authority(
                    tenant="tenant-a",
                    household=home,
                    devices=(f"{home}.light",),
                )
            ):
                result = await read_resource(server, RESOURCE)
            assert {row["device_id"] for row in result["rows"]} == {f"{home}.light"}
            assert {route["provider_id"] for row in result["rows"] for route in row["routes"]} == {
                f"{home}-provider",
            }
            other = "home-b" if home == "home-a" else "home-a"
            assert other not in json.dumps(result)
            with request_authority(scoped_authority(tenant="tenant-a", household=other)):
                denied = await read_resource(server, RESOURCE)
            assert denied["error"]["code"] == "insufficient_scope"
            assert "families" not in denied


async def test_imported_recipe_stays_unknown_and_reads_have_no_io_or_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, server = await build_configured_server(
        Settings(database_path=tmp_path / "recipe.sqlite3"),
        control_plane_mode=True,
    )
    try:
        recipe = _recipe()
        report = ConnectorQualificationService().report_for_recipe(recipe)
        assert report.software_status == "pass"
        assert all(gate.status == "pending" for gate in report.external_gates)
        transport = InMemoryMqttTransport([MqttMessage("composition/lamp-1/state", b"true")])
        provider = RecipeConnectionProvider(
            ConnectionProviderManifest(
                connector_id=recipe.connector_id,
                display_name=recipe.display_name,
                protocol="mqtt",
                version="1.0",
                recipe=recipe,
            ),
            discovery_engines={"manual": _ManualDiscovery()},
            transport_engines={"mqtt": MqttRecipeTransport(transport)},
            allowed_hosts=frozenset({"broker.local"}),
            allowed_ports=frozenset({1883}),
        )
        assert runtime.connection_provider_registry is not None
        runtime.connection_provider_registry.register(provider)
        candidate = ConnectionCandidate(
            connector_id=recipe.connector_id,
            candidate_id="lamp-1",
            display_name="Lamp",
            endpoint={"host": "broker.local", "port": 1883},
        )
        assert runtime.connection_manager is not None
        await runtime.connection_manager.create(
            ConnectionSetup(
                candidate=candidate,
                draft=ConnectionDraft(
                    connector_id=recipe.connector_id,
                    unique_id="lamp-1",
                    endpoint=candidate.endpoint,
                    options={},
                ),
            )
        )
        assert runtime.registry.devices
        spies = []
        for target, names in (
            (runtime.discovery, ("refresh",)),
            (runtime.adapter, ("discover", "read_state", "health", "execute")),
        ):
            for name in names:
                spy = AsyncMock(side_effect=AssertionError("coverage cannot call adapter IO"))
                monkeypatch.setattr(target, name, spy)
                spies.append(spy)
        sql: list[str] = []
        databases = (runtime.database, runtime.approval_database, runtime.audit_database)
        for database in databases:
            database.connection.set_trace_callback(sql.append)
        events = runtime.audit.events
        revision = runtime.state_store.runtime_revision
        profiles_before = await runtime.connection_manager.list()
        sql.clear()
        first = await read_resource(server, RESOURCE)
        second = await read_resource(server, RESOURCE)
        assert first == second
        assert first["rows"]
        for row in first["rows"]:
            assert_unknown(row)
        assert sql == []
        assert runtime.audit.events == events
        assert runtime.state_store.runtime_revision == revision
        assert transport.published == []
        for spy in spies:
            spy.assert_not_called()
        for database in databases:
            database.connection.set_trace_callback(None)
        assert await runtime.connection_manager.list() == profiles_before
    finally:
        await runtime.close()
