import pytest
from pydantic import ValidationError

from domoai.adapters.mqtt.config import MqttAdapterMapping


def test_mapping_rejects_a_wildcard_write_topic() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "state_topic": "home/lamp/state",
                                "command_topic": "home/lamp/+/set",
                            }
                        ],
                    }
                ],
            }
        )


def test_mapping_rejects_required_readback_without_feedback_topic() -> None:
    with pytest.raises(ValidationError, match="feedback"):
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "state_topic": "home/lamp/state",
                                "command_topic": "home/lamp/set",
                                "require_readback": True,
                            }
                        ],
                    }
                ],
            }
        )


def test_mapping_rejects_a_wildcard_feedback_topic() -> None:
    with pytest.raises(ValidationError, match="wildcard"):
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "state_topic": "home/lamp/state",
                                "command_topic": "home/lamp/set",
                                "feedback_topic": "home/lamp/+/state",
                                "require_readback": True,
                            }
                        ],
                    }
                ],
            }
        )


def test_mapping_preserves_typed_capability_contract_and_semantic_commands() -> None:
    mapping = MqttAdapterMapping.model_validate(
        {
            "schema_version": "v1",
            "adapter_id": "mqtt",
            "devices": [
                {
                    "source_id": "esp.thermostat",
                    "type": "climate",
                    "capabilities": [
                        {
                            "name": "temperature",
                            "kind": "number",
                            "unit": "°C",
                            "minimum": -40,
                            "maximum": 125,
                            "state_topic": "home/thermostat/temperature",
                        },
                        {
                            "name": "target_temperature",
                            "kind": "number",
                            "unit": "°C",
                            "minimum": 16,
                            "maximum": 30,
                            "state_topic": "home/thermostat/target",
                            "command_topic": "home/thermostat/target/set",
                            "commands": ["set_temperature"],
                        },
                    ],
                }
            ],
        }
    )

    temperature = mapping.devices[0].capabilities[0]
    target = mapping.devices[0].capabilities[1]
    assert temperature.kind.value == "number"
    assert temperature.minimum == -40
    assert temperature.maximum == 125
    assert temperature.unit == "°C"
    assert target.commands == ["set_temperature"]


def test_mapping_rejects_invalid_numeric_bounds() -> None:
    with pytest.raises(ValidationError, match="minimum"):
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.sensor",
                        "type": "sensor",
                        "capabilities": [
                            {
                                "name": "temperature",
                                "kind": "number",
                                "minimum": 100,
                                "maximum": 0,
                                "state_topic": "home/sensor/temperature",
                            }
                        ],
                    }
                ],
            }
        )


def test_mapping_rejects_duplicate_source_identity() -> None:
    with pytest.raises(ValidationError, match="duplicate MQTT source_id"):
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {"name": "power", "state_topic": "home/lamp/state"}
                        ],
                    },
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {"name": "brightness", "state_topic": "home/lamp/brightness"}
                        ],
                    },
                ],
            }
        )


def test_mapping_rejects_ambiguous_writable_semantic_command() -> None:
    with pytest.raises(ValidationError, match="ambiguous"):
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.panel",
                        "type": "climate",
                        "capabilities": [
                            {
                                "name": "target_temperature",
                                "kind": "number",
                                "state_topic": "home/panel/target",
                                "command_topic": "home/panel/target/set",
                                "commands": ["set_value"],
                            },
                            {
                                "name": "humidity_target",
                                "kind": "number",
                                "state_topic": "home/panel/humidity",
                                "command_topic": "home/panel/humidity/set",
                                "commands": ["set_value"],
                            },
                        ],
                    }
                ],
            }
        )
