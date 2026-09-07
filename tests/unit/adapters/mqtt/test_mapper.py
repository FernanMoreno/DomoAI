from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.mqtt.mapper import MqttMapper


def test_mapper_exposes_only_declared_mqtt_capabilities() -> None:
    mapping = MqttAdapterMapping.model_validate(
        {
            "schema_version": "v1",
            "adapter_id": "mqtt",
            "devices": [
                {
                    "source_id": "esp.lamp",
                    "type": "light",
                    "capabilities": [{"name": "power", "state_topic": "home/lamp/state"}],
                }
            ],
        }
    )

    snapshot = MqttMapper().to_snapshot(mapping)

    assert snapshot.source_entities[0]["entity_id"] == "esp.lamp"
    assert snapshot.source_entities[0]["domain"] == "light"
    assert [item["name"] for item in snapshot.source_entities[0]["capabilities"]] == ["power"]


def test_mapper_exposes_typed_ranges_units_and_semantic_commands() -> None:
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
                            "name": "target_temperature",
                            "kind": "number",
                            "unit": "°C",
                            "minimum": 16,
                            "maximum": 30,
                            "state_topic": "home/thermostat/target",
                            "command_topic": "home/thermostat/target/set",
                            "commands": ["set_temperature"],
                        }
                    ],
                }
            ],
        }
    )

    capability = MqttMapper().to_snapshot(mapping).source_entities[0]["capabilities"][0]

    assert capability == {
        "name": "target_temperature",
        "kind": "number",
        "unit": "°C",
        "readable": True,
        "writable": True,
        "minimum": 16,
        "maximum": 30,
        "commands": ["set_temperature"],
    }
