"""Wire-level generic MQTT discovery against disposable Mosquitto."""

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from domoai.adapters.mqtt.adapter import GenericMqttAdapter
from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport

pytest.importorskip("testcontainers", reason="testcontainers is a dev-only dependency")
from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.wait_strategies import LogMessageWaitStrategy  # noqa: E402


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _docker_available(), reason="Docker daemon unavailable")


@pytest_asyncio.fixture
async def broker_port() -> AsyncIterator[int]:
    container = DockerContainer("eclipse-mosquitto:1.6.15").with_exposed_ports(1883).waiting_for(
        LogMessageWaitStrategy("mosquitto version").with_startup_timeout(30)
    )
    container.start()
    try:
        yield int(container.get_exposed_port(1883))
    finally:
        container.stop()


@pytest.mark.asyncio
@pytest.mark.composition
async def test_generic_mqtt_discovers_declared_esp_route_over_broker(broker_port: int) -> None:
    host = "127.0.0.1"
    publisher = AiomqttTransport(host, port=broker_port, timeout=5)
    await publisher.connect()
    await publisher.publish("home/esp-lamp/state", b"true", retained=True)
    await publisher.disconnect()

    adapter = GenericMqttAdapter(
        AiomqttTransport(host, port=broker_port, timeout=5),
        MqttAdapterMapping.model_validate(
            {
                "schema_version": "v1",
                "adapter_id": "mqtt",
                "devices": [
                    {
                        "source_id": "esp.lamp",
                        "type": "light",
                        "capabilities": [
                            {"name": "power", "state_topic": "home/esp-lamp/state"}
                        ],
                    }
                ],
            }
        ),
        discovery_timeout=2,
    )
    await adapter.connect()
    try:
        snapshot = await adapter.discover()
    finally:
        await adapter.disconnect()

    assert snapshot.source_states[0]["value"] is True
