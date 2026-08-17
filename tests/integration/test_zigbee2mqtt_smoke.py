import os
from urllib.parse import urlparse

import pytest

from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport


def build_live_adapter(url: str) -> Zigbee2MqttAdapter:
    parsed = urlparse(url)
    if parsed.scheme not in {"mqtt", "mqtts"} or parsed.hostname is None:
        raise ValueError("DOMOAI_ZIGBEE2MQTT_URL must be a valid mqtt:// URL")
    if parsed.scheme == "mqtts":
        raise ValueError("The live Zigbee2MQTT smoke does not support mqtts:// yet")

    timeout = float(os.getenv("DOMOAI_MQTT_TIMEOUT_SECONDS", "5"))
    return Zigbee2MqttAdapter(
        AiomqttTransport(
            parsed.hostname,
            port=parsed.port or 1883,
            username=os.getenv("DOMOAI_MQTT_USERNAME"),
            password=os.getenv("DOMOAI_MQTT_PASSWORD"),
            timeout=timeout,
        ),
        base_topic=os.getenv("DOMOAI_ZIGBEE2MQTT_BASE_TOPIC", "zigbee2mqtt"),
        discovery_timeout=timeout,
    )


@pytest.mark.asyncio
async def test_live_zigbee2mqtt_smoke_is_opt_in() -> None:
    url = os.getenv("DOMOAI_ZIGBEE2MQTT_URL")
    if not url:
        pytest.skip("Set DOMOAI_ZIGBEE2MQTT_URL to run the live Zigbee2MQTT smoke test")
    adapter = build_live_adapter(url)
    await adapter.connect()
    try:
        snapshot = await adapter.discover()
        assert snapshot.source_entities
    finally:
        await adapter.disconnect()
