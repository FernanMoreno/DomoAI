from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.connector_recipes import (
    KNX_RECIPE_DPTS,
    MATTER_CLUSTER_ATTRIBUTES,
    KnxRecipeBinding,
    KnxRecipeEntity,
    KnxRecipeProfile,
    MatterRecipeBinding,
    MatterRecipeEndpoint,
    MatterRecipeProfile,
    ModbusRecipeBinding,
    ModbusRecipeEntity,
    ModbusRecipePoint,
    ModbusRecipeProfile,
)


def modbus_profile() -> ModbusRecipeProfile:
    return ModbusRecipeProfile(
        entities=(
            ModbusRecipeEntity(
                entity_id="living-room-light",
                name="Living room light",
                semantic_type="light",
                unit_id=1,
                capabilities=(
                    ModbusRecipeBinding(
                        name="power",
                        state=ModbusRecipePoint(area="coil", address=0, data_type="bool"),
                        command=ModbusRecipePoint(area="coil", address=0, data_type="bool"),
                        readback=ModbusRecipePoint(area="coil", address=0, data_type="bool"),
                    ),
                ),
            ),
        ),
    )


def knx_profile() -> KnxRecipeProfile:
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


def matter_profile() -> MatterRecipeProfile:
    return MatterRecipeProfile(
        endpoints=(
            MatterRecipeEndpoint(
                endpoint_id=1,
                device_type_ids=(0x0100,),
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
        node_ids=(12,),
    )


def test_protocol_profiles_have_discriminators_and_public_dump() -> None:
    assert modbus_profile().transport == "modbus"
    assert knx_profile().transport == "knx"
    assert matter_profile().transport == "matter"
    assert "client_secret" not in matter_profile().model_dump_json()
    assert KNX_RECIPE_DPTS["temperature"] == "9.001"
    assert MATTER_CLUSTER_ATTRIBUTES["power"] == (6, 0)


def test_protocol_profiles_reject_duplicate_sources_and_unsupported_semantics() -> None:
    profile = modbus_profile()
    duplicate = profile.entities[0].model_copy(update={"entity_id": "living-room-light"})
    with pytest.raises(ValidationError, match="duplicate"):
        ModbusRecipeProfile(entities=(profile.entities[0], duplicate))

    with pytest.raises(ValidationError, match="unsupported capability"):
        KnxRecipeEntity(
            entity_id="sensor",
            name="Sensor",
            semantic_type="sensor",
            capabilities=(
                KnxRecipeBinding(
                    name="power",
                    dpt="1.001",
                    state_group_address="1/1/1",
                ),
            ),
        )


def test_matter_profile_requires_readback_for_writes_and_known_mapping() -> None:
    with pytest.raises(ValidationError, match="readback"):
        MatterRecipeBinding(
            name="power", cluster_id=6, attribute_id=0, command_names=("On",)
        )
    with pytest.raises(ValidationError, match="cluster"):
        MatterRecipeBinding(
            name="power",
            cluster_id=999,
            attribute_id=0,
            command_names=(),
        )

