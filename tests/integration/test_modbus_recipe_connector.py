from __future__ import annotations

import pytest

from domoai.adapters.modbus.recipe_adapter import ModbusRecipeTransport
from domoai.adapters.modbus.recipe_discovery import ModbusRecipeDiscovery
from domoai.adapters.modbus.transport import InMemoryModbusTransport
from domoai.domain.connections import ConnectionDraft
from domoai.domain.connector_recipes import (
    CapabilityBinding,
    ConnectorRecipe,
    ModbusRecipeBinding,
    ModbusRecipeEntity,
    ModbusRecipePoint,
    ModbusRecipeProfile,
    TransportFamily,
)
from domoai.runtime.connection_ports import ConnectionSetup


def _recipe() -> ConnectorRecipe:
    profile = ModbusRecipeProfile(
        entities=(
            ModbusRecipeEntity(
                entity_id="pump",
                name="Pump",
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
        connector_id="fixture-modbus-recipe",
        display_name="Fixture Modbus recipe",
        transport_family=TransportFamily.MODBUS,
        capabilities=(
            CapabilityBinding(
                capability_id="power", kind="boolean", read_path="register://power"
            ),
        ),
        protocol_profile=profile,
    )


@pytest.mark.asyncio
async def test_modbus_recipe_engine_builds_from_injected_transport_and_discovery_is_manual(
) -> None:
    transport = InMemoryModbusTransport()
    recipe = _recipe()
    candidate = ModbusRecipeDiscovery().manual_candidates(
        recipe.connector_id, "gateway.local", 502
    )[0]
    candidate_setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id=recipe.connector_id,
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={},
        ),
    )
    # The engine only needs the draft endpoint; the discovery fixture above is
    # intentionally bounded and does not contact a Modbus unit.
    adapter = await ModbusRecipeTransport(
        transport_factory=lambda _host, _port: transport
    ).build_adapter(recipe, candidate_setup, adapter_id="recipe:modbus")
    assert adapter.adapter_id == "recipe:modbus"
