import json
from pathlib import Path

from jsonschema import Draft202012Validator

from domoai.adapters.mqtt.config import MqttAdapterMapping
from tests.fixtures.generic_mqtt import mapping

ROOT = Path(__file__).parents[2]


def test_mqtt_mapping_schema_accepts_the_canonical_fixture() -> None:
    schema = json.loads(
        (ROOT / "schemas/v1/mqtt-adapter-mapping.schema.json").read_text(encoding="utf-8")
    )
    document = mapping().model_dump(mode="json")

    Draft202012Validator(schema).validate(document)
    assert MqttAdapterMapping.model_validate(document).adapter_id == "mqtt"
