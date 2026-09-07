from __future__ import annotations

from pathlib import Path

import pytest

from domoai.adapters.knx.config import load_mapping
from domoai.lab.knx_bridge import knx_command_to_mqtt, state_to_knx_writes

MAPPING = load_mapping(Path("dev/lab/configs/knx-battery-virtual.json"))


def test_state_projects_to_configured_knx_groups() -> None:
    writes = state_to_knx_writes({"soc_kwh": 5.0, "power_kw": -1.5, "capacity_kwh": 10.0}, MAPPING)

    assert [(item.group_address, item.dpt, item.value) for item in writes] == [
        ("4/0/2", "9.024", -1.5),
        ("4/0/1", "13.013", 5.0),
        ("4/0/3", "13.013", 10.0),
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2, b"2"), (-1.5, b"-1.5"), (0.0, b"0")],
)
def test_knx_command_is_a_signed_mqtt_setpoint(value: float, expected: bytes) -> None:
    assert knx_command_to_mqtt(value) == expected


def test_bridge_rejects_incomplete_state() -> None:
    with pytest.raises(ValueError, match="soc_kwh"):
        state_to_knx_writes({"soc_kwh": None}, MAPPING)


def test_bridge_rejects_boolean_command() -> None:
    with pytest.raises(ValueError, match="numeric"):
        knx_command_to_mqtt(True)
