from __future__ import annotations

import ssl
from collections.abc import Iterator

import pytest

from domoai.adapters.zigbee2mqtt.connection import (
    BrokerTransportConfig,
    Zigbee2MqttConnectionProvider,
)
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDraft,
    InventoryStatus,
    ProductStatus,
    TransportStatus,
)
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.mqtt_transport import InMemoryMqttTransport
from tests.fixtures.zigbee2mqtt import json_message, retained_messages

SECRET_REF = "secret:00000000-0000-0000-0000-000000000001"


class RecordingSecretResolver:
    def __init__(self, value: str = "mqtt-password") -> None:
        self.value = value
        self.references: list[str] = []

    def resolve(self, reference: str) -> str:
        self.references.append(reference)
        return self.value


def _provider(
    *,
    transport_factory=None,
    broker_discoverer=None,
    resolver: RecordingSecretResolver | None = None,
) -> Zigbee2MqttConnectionProvider:
    return Zigbee2MqttConnectionProvider(
        secret_resolver=resolver or RecordingSecretResolver(),
        transport_factory=transport_factory,
        broker_discoverer=broker_discoverer,
        timeout_seconds=0.02,
    )


def _setup(
    provider: Zigbee2MqttConnectionProvider,
    url: str = "mqtt://broker.local",
    *,
    options: dict[str, object] | None = None,
    credential_ref: str | None = None,
) -> ConnectionSetup:
    candidate = provider.manual_candidate(url)
    return ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="zigbee2mqtt",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options=options or {},
            credential_ref=credential_ref,
        ),
    )


def test_manual_candidate_normalizes_mqtt_defaults_and_rejects_url_credentials() -> None:
    provider = _provider()

    mqtt = provider.manual_candidate(" mqtt://broker.local ")
    mqtts = provider.manual_candidate("mqtts://secure.local")

    assert mqtt.endpoint == {
        "url": "mqtt://broker.local:1883",
        "host": "broker.local",
        "port": 1883,
        "tls": False,
    }
    assert mqtts.endpoint["port"] == 8883
    assert mqtts.endpoint["tls"] is True
    with pytest.raises(ValueError, match="credentials"):
        provider.manual_candidate("mqtt://operator:password@broker.local")
    with pytest.raises(ValueError, match="scheme"):
        provider.manual_candidate("http://broker.local")


@pytest.mark.asyncio
async def test_discovery_is_optional_and_validates_connector_identity() -> None:
    provider_without_discovery = _provider()
    assert await provider_without_discovery.discover() == []

    candidate = provider_without_discovery.manual_candidate("mqtt://discovered.local")
    provider = _provider(broker_discoverer=lambda: [candidate])
    assert await provider.discover() == [candidate]

    wrong = ConnectionCandidate(
        connector_id="other",
        candidate_id="other:1",
        display_name="Other",
        endpoint={"host": "other"},
    )
    with pytest.raises(ValueError, match="incompatible"):
        await _provider(broker_discoverer=lambda: [wrong]).discover()


@pytest.mark.asyncio
async def test_probe_detects_retained_bridge_and_base_topic() -> None:
    transport = InMemoryMqttTransport(
        [
            json_message("zigbee2mqtt/bridge/state", {"state": "online"}, retained=True),
            json_message("zigbee2mqtt/bridge/devices", [], retained=True),
        ]
    )
    provider = _provider(transport_factory=lambda _: transport)
    candidate = provider.manual_candidate("mqtt://broker.local")
    candidate = candidate.model_copy(
        update={"endpoint": {**candidate.endpoint, "url": "mqtt://broker.local:1883"}}
    )

    result = await provider.probe(candidate)

    assert result.transport_status is TransportStatus.REACHABLE
    assert result.inventory_status is InventoryStatus.EMPTY
    assert result.product_status is ProductStatus.CONNECTED
    assert transport.subscriptions == ["+/bridge/#"]
    assert transport.connected is False


@pytest.mark.asyncio
async def test_validate_resolves_password_only_at_transport_boundary_and_normalizes_topic() -> None:
    resolver = RecordingSecretResolver()
    configs: list[BrokerTransportConfig] = []

    def factory(config: BrokerTransportConfig) -> InMemoryMqttTransport:
        configs.append(config)
        return InMemoryMqttTransport(
            [
                json_message("home/bridge/state", {"state": "online"}, retained=True),
                json_message("home/bridge/info", {"version": "1.40.0"}, retained=True),
            ]
        )

    provider = _provider(transport_factory=factory, resolver=resolver)
    setup = _setup(
        provider,
        options={"username": "operator", "timeout_seconds": 0.02},
        credential_ref=SECRET_REF,
    )

    result = await provider.validate(setup)

    assert result.valid is True
    assert result.normalized_draft is not None
    assert result.normalized_draft.options["base_topic"] == "home"
    assert resolver.references == [SECRET_REF]
    assert configs[0].password == "mqtt-password"
    assert "mqtt-password" not in repr(result)
    assert "mqtt-password" not in repr(setup)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("messages", "status", "code"),
    [
        (
            [json_message("zigbee2mqtt/bridge/state", {"state": "offline"}, retained=True)],
            ProductStatus.DEGRADED,
            "bridge_offline",
        ),
        (
            [json_message("unrelated/topic", {"state": "online"}, retained=True)],
            ProductStatus.UNAVAILABLE,
            "zigbee2mqtt_not_detected",
        ),
    ],
)
async def test_validate_rejects_offline_or_missing_bridge(
    messages: list, status: ProductStatus, code: str
) -> None:
    provider = _provider(transport_factory=lambda _: InMemoryMqttTransport(messages))

    result = await provider.validate(_setup(provider))

    assert result.valid is False
    assert result.diagnostics[0].code == code
    if code == "bridge_offline":
        assert status is ProductStatus.DEGRADED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PermissionError("broker rejected credentials"), "authentication_failed"),
        (ssl.SSLError("certificate verify failed"), "tls_certificate_invalid"),
        (TimeoutError("broker timeout"), "timeout"),
    ],
)
async def test_validate_maps_transport_errors_to_sanitized_diagnostics(
    error: Exception, code: str
) -> None:
    async def failing_factory(_: BrokerTransportConfig) -> InMemoryMqttTransport:
        raise error

    provider = _provider(transport_factory=failing_factory)
    result = await provider.validate(_setup(provider))

    assert result.valid is False
    assert result.diagnostics[0].code == code
    assert "broker rejected credentials" not in result.diagnostics[0].message
    assert "certificate verify failed" not in result.diagnostics[0].message


@pytest.mark.asyncio
async def test_setup_rejects_incomplete_credentials_and_insecure_tls() -> None:
    provider = _provider()

    incomplete = await provider.validate(
        _setup(provider, options={"username": "operator"})
    )
    assert incomplete.valid is False
    assert incomplete.diagnostics[0].code == "credential_required"

    insecure = await provider.validate(
        _setup(provider, "mqtts://broker.local", options={"tls_insecure": True})
    )
    assert insecure.valid is False
    assert insecure.diagnostics[0].code == "tls_configuration_invalid"


@pytest.mark.asyncio
async def test_build_adapter_propagates_runtime_identity() -> None:
    transports: Iterator[InMemoryMqttTransport] = iter(
        [InMemoryMqttTransport(retained_messages()), InMemoryMqttTransport(retained_messages())]
    )
    provider = _provider(transport_factory=lambda _: next(transports))
    adapter = await provider.build_adapter(_setup(provider), adapter_id="connection:one")

    assert adapter.adapter_id == "connection:one"
    assert adapter.base_topic == "zigbee2mqtt"
