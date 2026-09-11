from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.modbus.recipe_adapter import ModbusRecipeAdapter
from domoai.adapters.modbus.transport import InMemoryModbusTransport, ModbusSample
from domoai.domain.connector_recipes import (
    ModbusRecipeBinding,
    ModbusRecipeEntity,
    ModbusRecipePoint,
    ModbusRecipeProfile,
)
from domoai.domain.models import Command, SourceRef


def _profile() -> ModbusRecipeProfile:
    return ModbusRecipeProfile(
        entities=(
            ModbusRecipeEntity(
                entity_id="living-room-light",
                name="Living room light",
                semantic_type="light",
                area_id="living-room",
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


@pytest.mark.asyncio
async def test_modbus_recipe_adapter_discovers_reads_executes_and_reads_back() -> None:
    transport = InMemoryModbusTransport(
        samples=[ModbusSample(1, "coil", 0, (True,), datetime.now(UTC))]
    )
    transport.write_state_map[(1, "coil", 1)] = (1, "coil", 0)
    adapter = ModbusRecipeAdapter(transport, _profile(), adapter_id="recipe:modbus")

    await adapter.connect()
    snapshot = await adapter.discover()
    assert snapshot.source_entities[0]["entity_id"] == "living-room-light"
    assert snapshot.source_entities[0]["capabilities"][0]["name"] == "power"

    source = SourceRef(adapter_id=adapter.adapter_id, external_id="living-room-light")
    state = await adapter.read_state([source])
    assert state[0].value is True

    ack = await adapter.execute_source(
        Command(
            id="cmd-1",
            device_id="living-room.living-room-light",
            command="turn_off",
            idempotency_key="idem-1",
        ),
        "living-room-light",
    )
    assert ack.accepted
    assert transport.writes[-1].address == 1
    assert (await adapter.read_state([source]))[0].value is False


@pytest.mark.asyncio
async def test_modbus_recipe_adapter_does_not_accept_undeclared_commands() -> None:
    transport = InMemoryModbusTransport(
        samples=[ModbusSample(1, "coil", 0, (True,), datetime.now(UTC))]
    )
    adapter = ModbusRecipeAdapter(transport, _profile())
    await adapter.connect()
    await adapter.discover()
    ack = await adapter.execute_source(
        Command(
            id="cmd-2",
            device_id="living-room.living-room-light",
            command="set_brightness",
            value=40,
            idempotency_key="idem-2",
        ),
        "living-room-light",
    )
    assert not ack.accepted
    assert transport.writes == []
