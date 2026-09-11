from __future__ import annotations

import pytest

from domoai.adapters.matter.recipe_adapter import MatterRecipeAdapter
from domoai.adapters.matter.transport import InMemoryMatterTransport
from domoai.domain.connector_recipes import (
    MatterRecipeBinding,
    MatterRecipeEndpoint,
    MatterRecipeProfile,
)
from domoai.domain.models import Command, SourceRef
from tests.fixtures.matter_server import node_snapshot, server_info


def _profile() -> MatterRecipeProfile:
    return MatterRecipeProfile(
        endpoints=(
            MatterRecipeEndpoint(
                endpoint_id=1,
                device_type_ids=(0x0101,),
                capabilities=(
                    MatterRecipeBinding(
                        name="power",
                        cluster_id=6,
                        attribute_id=0,
                        command_names=("On", "Off", "Toggle"),
                        readback=True,
                    ),
                ),
            ),
        ),
        node_ids=(42,),
    )


@pytest.mark.asyncio
async def test_matter_recipe_adapter_filters_endpoint_capabilities_and_commands() -> None:
    transport = InMemoryMatterTransport(
        nodes=[node_snapshot(42, profile="dimmable_light")], server_info=server_info()
    )
    adapter = MatterRecipeAdapter(transport, _profile(), adapter_id="recipe:matter")

    await adapter.connect()
    snapshot = await adapter.discover()
    assert len(snapshot.source_entities) == 1
    assert [item["name"] for item in snapshot.source_entities[0]["capabilities"]] == ["power"]
    assert all(item["capability"] == "power" for item in snapshot.source_states)

    source = SourceRef(adapter_id=adapter.adapter_id, external_id="node:42/endpoint:1")
    assert (await adapter.read_state([source]))[0].value is True
    ack = await adapter.execute_source(
        Command(
            id="cmd-1",
            device_id="unassigned.matter-fixture-42",
            command="turn_off",
            idempotency_key="idem-1",
        ),
        "node:42/endpoint:1",
    )
    assert ack.accepted
    assert transport.requests[-1].command == "device_command"
    assert transport.requests[-1].args["cluster_id"] == 6


@pytest.mark.asyncio
async def test_matter_recipe_adapter_rejects_unprofiled_brightness() -> None:
    transport = InMemoryMatterTransport(
        nodes=[node_snapshot(42, profile="dimmable_light")], server_info=server_info()
    )
    adapter = MatterRecipeAdapter(transport, _profile())
    await adapter.connect()
    await adapter.discover()
    ack = await adapter.execute_source(
        Command(
            id="cmd-2",
            device_id="unassigned.matter-fixture-42",
            command="set_brightness",
            value=50,
            idempotency_key="idem-2",
        ),
        "node:42/endpoint:1",
    )
    assert not ack.accepted
    assert all(request.command != "device_command" for request in transport.requests)

