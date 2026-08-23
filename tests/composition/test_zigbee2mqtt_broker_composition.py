"""Zigbee2MQTT <-> real MQTT broker composition test.

`tests/integration/test_zigbee2mqtt_fixture.py` proves the adapter's
domain logic against `InMemoryMqttTransport`. That mock can never catch a
wire-level bug: wrong topic wildcard, retained-flag handling, or QoS
mismatch between `AiomqttTransport` and an actual broker. This test
replaces the mock with a disposable real Eclipse Mosquitto container (the
same messages as `tests/fixtures/zigbee2mqtt.retained_messages`, published
over the wire) and asserts discovery reaches the same registry outcome.

Skips outright if the Docker daemon is unreachable, matching this
repository's existing opt-in-live-infra convention (see
`tests/integration/test_zigbee2mqtt_smoke.py`) rather than failing CI hosts
without Docker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport
from domoai.application.discovery_service import DiscoveryService
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.zigbee2mqtt import retained_messages

pytest.importorskip("testcontainers", reason="testcontainers is a dev-only dependency")

from testcontainers.core.container import DockerContainer  # noqa: E402
from testcontainers.core.waiting_utils import wait_for_logs  # noqa: E402


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon not reachable; skipping real-broker composition test",
)


@pytest_asyncio.fixture
async def mosquitto_port() -> AsyncIterator[int]:
    container = DockerContainer("eclipse-mosquitto:1.6.15").with_exposed_ports(1883)
    container.start()
    try:
        wait_for_logs(container, "mosquitto version", timeout=30)
        yield int(container.get_exposed_port(1883))
    finally:
        container.stop()


async def _publish_fixture(host: str, port: int) -> None:
    publisher = AiomqttTransport(host, port=port, timeout=5.0)
    await publisher.connect()
    try:
        for message in retained_messages():
            await publisher.publish(message.topic, message.payload, retained=message.retained)
    finally:
        await publisher.disconnect()


@pytest.mark.asyncio
@pytest.mark.composition
async def test_discovery_over_real_mosquitto_matches_fixture_registry(
    mosquitto_port: int,
) -> None:
    host = "127.0.0.1"
    await _publish_fixture(host, mosquitto_port)

    adapter = Zigbee2MqttAdapter(
        AiomqttTransport(host, port=mosquitto_port, timeout=5.0),
        discovery_timeout=2.0,
    )
    await adapter.connect()
    try:
        registry = DeviceRegistry()
        discovery = DiscoveryService(adapter, registry, StateStore(), AuditLog())
        snapshot = await discovery.refresh()
    finally:
        await adapter.disconnect()

    assert len(snapshot.devices) == 3
    assert all(device.protocol == "zigbee2mqtt" for device in snapshot.devices)
    health = await adapter.health()
    assert health.connected is True
