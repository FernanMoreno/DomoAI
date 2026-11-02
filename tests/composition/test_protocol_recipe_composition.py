from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.knx.recipe_adapter import KnxRecipeTransport
from domoai.adapters.knx.transport import InMemoryKnxTransport, KnxGroupValue
from domoai.adapters.modbus.recipe_adapter import ModbusRecipeTransport
from domoai.adapters.modbus.transport import InMemoryModbusTransport, ModbusSample
from domoai.application.connection_manager import ConnectionManager
from domoai.application.connection_providers import ConnectionProviderRegistry
from domoai.application.connector_recipes import RecipeConnectionProvider
from domoai.application.discovery_service import DiscoveryService
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.domain.connector_recipes import (
    CapabilityBinding,
    ConnectorRecipe,
    KnxRecipeBinding,
    KnxRecipeEntity,
    KnxRecipeProfile,
    ModbusRecipeBinding,
    ModbusRecipeEntity,
    ModbusRecipePoint,
    ModbusRecipeProfile,
)
from domoai.domain.coordination import LeaseScope
from domoai.mcp.resources import inventory_snapshot
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.connection_ports import ConnectionProviderManifest, ConnectionSetup
from domoai.runtime.events import AuditLog
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def _modbus_recipe() -> ConnectorRecipe:
    profile = ModbusRecipeProfile(
        entities=(
            ModbusRecipeEntity(
                entity_id="pump",
                name="Pump",
                area_id="garage",
                semantic_type="switch",
                unit_id=1,
                capabilities=(
                    ModbusRecipeBinding(
                        name="power",
                        state=ModbusRecipePoint(area="coil", address=0, data_type="bool"),
                        command=ModbusRecipePoint(area="coil", address=1, data_type="bool"),
                        readback=ModbusRecipePoint(area="coil", address=0, data_type="bool"),
                    ),
                ),
            ),
        ),
    )
    return ConnectorRecipe(
        connector_id="composition_modbus",
        display_name="Composition Modbus",
        transport_family="modbus",
        capabilities=(
            CapabilityBinding(capability_id="power", kind="boolean", read_path="modbus://power"),
        ),
        protocol_profile=profile,
    )


def _knx_recipe() -> ConnectorRecipe:
    profile = KnxRecipeProfile(
        entities=(
            KnxRecipeEntity(
                entity_id="light",
                name="Light",
                area_id="living",
                semantic_type="switch",
                capabilities=(
                    KnxRecipeBinding(
                        name="power",
                        dpt="1.001",
                        state_group_address="1/1/1",
                        command_group_address="1/1/2",
                        readback_group_address="1/1/1",
                    ),
                ),
            ),
        ),
    )
    return ConnectorRecipe(
        connector_id="composition_knx",
        display_name="Composition KNX",
        transport_family="knx",
        capabilities=(
            CapabilityBinding(capability_id="power", kind="boolean", read_path="knx://power"),
        ),
        protocol_profile=profile,
    )


def _setup(recipe: ConnectorRecipe, endpoint: dict[str, object]) -> ConnectionSetup:
    candidate = ConnectionCandidate(
        connector_id=recipe.connector_id,
        candidate_id=f"{recipe.connector_id}:fixture",
        display_name=recipe.display_name,
        endpoint=endpoint,
    )
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id=recipe.connector_id,
            unique_id=candidate.candidate_id,
            endpoint=endpoint,
            options={},
        ),
    )


@pytest.mark.asyncio
async def test_modbus_and_knx_recipe_engines_share_runtime_registry_state_and_mcp_inventory(
    tmp_path,
) -> None:
    observed = datetime(2026, 9, 9, tzinfo=UTC)
    modbus_transport = InMemoryModbusTransport(
        samples=[ModbusSample(1, "coil", 0, (True,), observed)]
    )
    modbus_transport.write_state_map[(1, "coil", 1)] = (1, "coil", 0)
    knx_transport = InMemoryKnxTransport(incoming=[KnxGroupValue("1/1/1", "1.001", True, observed)])
    knx_transport.write_state_map[("1/1/2", "1.001")] = ("1/1/1", "1.001")
    modbus = _modbus_recipe()
    knx = _knx_recipe()
    providers = ConnectionProviderRegistry()
    providers.register(
        RecipeConnectionProvider(
            ConnectionProviderManifest(
                connector_id=modbus.connector_id,
                display_name=modbus.display_name,
                protocol="modbus",
                version="1",
                recipe=modbus,
            ),
            transport_engines={
                "modbus": ModbusRecipeTransport(lambda _host, _port: modbus_transport)
            },
            allowed_hosts=frozenset({"modbus.local"}),
            allowed_ports=frozenset({502}),
        )
    )
    providers.register(
        RecipeConnectionProvider(
            ConnectionProviderManifest(
                connector_id=knx.connector_id,
                display_name=knx.display_name,
                protocol="knx",
                version="1",
                recipe=knx,
            ),
            transport_engines={
                "knx": KnxRecipeTransport(lambda _host, _port, _secure: knx_transport)
            },
            allowed_hosts=frozenset({"knx.local"}),
            allowed_ports=frozenset({3671}),
        )
    )
    registry = DeviceRegistry()
    runtime = ManagedAdapterSet(registry=registry)
    state_store = StateStore()
    discovery = DiscoveryService(runtime, registry, state_store, AuditLog())
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    manager = ConnectionManager(
        repository=ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        ),
        providers=providers,
        runtime=runtime,
        refresh_inventory=discovery.refresh,
    )

    try:
        await manager.create(_setup(modbus, {"host": "modbus.local", "port": 502}))
        await manager.create(_setup(knx, {"host": "knx.local", "port": 3671}))
        assert {device.id for device in registry.devices} == {"garage.pump", "living.light"}
        assert state_store.peek("garage.pump", "power") is not None
        assert state_store.peek("living.light", "power") is not None
        mcp_inventory = inventory_snapshot(
            registry,
            runtime_revision=state_store.runtime_revision,
            refreshed_at=None,
        )
        assert {item["id"] for item in mcp_inventory["devices"]} == {
            "garage.pump",
            "living.light",
        }
    finally:
        await runtime.disconnect()
        await database.close()
