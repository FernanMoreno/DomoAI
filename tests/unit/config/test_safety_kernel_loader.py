from __future__ import annotations

from domoai.config.safety_kernel_loader import load_safety_limits, load_safety_limits_file
from domoai.domain.models import DeviceType


def test_load_safety_limits_from_list_payload() -> None:
    limits = load_safety_limits(
        [
            {
                "device_type": "ev_charger",
                "capability": "charging_current",
                "maximum": 32,
            }
        ]
    )

    assert len(limits) == 1
    assert limits[0].device_type is DeviceType.EV_CHARGER
    assert limits[0].capability == "charging_current"
    assert limits[0].maximum == 32


def test_load_safety_limits_from_mapping_payload_with_limits_key() -> None:
    limits = load_safety_limits(
        {
            "limits": [
                {
                    "device_type": "energy",
                    "capability": "battery_soc",
                    "minimum": 10,
                }
            ]
        }
    )

    assert len(limits) == 1
    assert limits[0].device_type is DeviceType.ENERGY
    assert limits[0].minimum == 10


def test_load_safety_limits_file(tmp_path) -> None:
    config_path = tmp_path / "safety_limits.toml"
    config_path.write_text(
        """
        [[limits]]
        device_type = "climate"
        capability = "target_temperature"
        minimum = 10
        maximum = 28
        """
    )

    limits = load_safety_limits_file(config_path)

    assert len(limits) == 1
    assert limits[0].device_type is DeviceType.CLIMATE
    assert limits[0].minimum == 10
    assert limits[0].maximum == 28
