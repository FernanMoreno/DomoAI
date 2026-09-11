from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.knx.recipe_adapter import KnxRecipeAdapter
from domoai.adapters.knx.transport import InMemoryKnxTransport, KnxGroupValue
from domoai.domain.connector_recipes import (
    KnxRecipeBinding,
    KnxRecipeEntity,
    KnxRecipeProfile,
)
from domoai.domain.models import Command, SourceRef


def _profile() -> KnxRecipeProfile:
    return KnxRecipeProfile(
        entities=(
            KnxRecipeEntity(
                entity_id="living-room-light",
                name="Living room light",
                semantic_type="light",
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


@pytest.mark.asyncio
async def test_knx_recipe_adapter_discovers_reads_executes_and_reads_back() -> None:
    transport = InMemoryKnxTransport(
        incoming=[KnxGroupValue("1/1/1", "1.001", True, datetime.now(UTC))]
    )
    transport.write_state_map[("1/1/2", "1.001")] = ("1/1/1", "1.001")
    adapter = KnxRecipeAdapter(transport, _profile(), adapter_id="recipe:knx")

    await adapter.connect()
    snapshot = await adapter.discover()
    assert snapshot.source_entities[0]["entity_id"] == "living-room-light"
    source = SourceRef(adapter_id=adapter.adapter_id, external_id="living-room-light")
    assert (await adapter.read_state([source]))[0].value is True

    ack = await adapter.execute_source(
        Command(
            id="cmd-1",
            device_id="unassigned.living-room-light",
            command="turn_off",
            idempotency_key="idem-1",
        ),
        "living-room-light",
    )
    assert ack.accepted
    assert transport.writes[-1].group_address == "1/1/2"
    assert (await adapter.read_state([source]))[0].value is False


@pytest.mark.asyncio
async def test_knx_recipe_adapter_rejects_undeclared_write() -> None:
    transport = InMemoryKnxTransport(
        incoming=[KnxGroupValue("1/1/1", "1.001", True, datetime.now(UTC))]
    )
    adapter = KnxRecipeAdapter(transport, _profile())
    await adapter.connect()
    await adapter.discover()
    ack = await adapter.execute_source(
        Command(
            id="cmd-2",
            device_id="unassigned.living-room-light",
            command="set_brightness",
            value=50,
            idempotency_key="idem-2",
        ),
        "living-room-light",
    )
    assert not ack.accepted
    assert transport.writes == []
