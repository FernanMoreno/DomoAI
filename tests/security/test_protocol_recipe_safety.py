from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.connector_recipes import (
    KnxRecipeBinding,
    MatterRecipeBinding,
    ModbusRecipeBinding,
    ModbusRecipePoint,
)


def test_modbus_profile_rejects_out_of_range_and_unsafe_points() -> None:
    with pytest.raises(ValidationError):
        ModbusRecipePoint(area="holding_register", address=65535, data_type="float32")
    with pytest.raises(ValidationError):
        ModbusRecipePoint(area="input_register", address=1, data_type="bool")
    with pytest.raises(ValidationError):
        ModbusRecipePoint(area="holding_register", address=1, data_type="uint16", scale=0)

    with pytest.raises(ValidationError):
        ModbusRecipeBinding(
            name="power",
            state=ModbusRecipePoint(
                area="holding_register", address=1, data_type="uint16"
            ),
        )


def test_knx_recipe_rejects_broadcast_zero_and_dpt_mismatch() -> None:
    with pytest.raises(ValidationError, match="broadcast"):
        KnxRecipeBinding(name="power", dpt="1.001", state_group_address="0/0/0")
    with pytest.raises(ValidationError, match="DPT"):
        KnxRecipeBinding(name="temperature", dpt="1.001", state_group_address="1/1/1")


def test_matter_recipe_rejects_vendor_cluster_and_vendor_command() -> None:
    with pytest.raises(ValidationError, match="standard"):
        MatterRecipeBinding(
            name="power", cluster_id=0xFFF1, attribute_id=0, command_names=()
        )
    with pytest.raises(ValidationError, match="allowlist"):
        MatterRecipeBinding(
            name="power", cluster_id=6, attribute_id=0, command_names=("VendorCommand",)
        )

