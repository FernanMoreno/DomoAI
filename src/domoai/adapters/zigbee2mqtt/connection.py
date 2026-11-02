"""Zigbee2MQTT broker onboarding over the shared MQTT transport boundary."""

from __future__ import annotations

import asyncio
import inspect
import json
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from domoai.adapters.zigbee2mqtt.adapter import Zigbee2MqttAdapter
from domoai.domain.connections import (
    ConnectionCandidate,
    ConnectionDiagnostic,
    DiagnosticSeverity,
    InventoryStatus,
    ProbeResult,
    ProductStatus,
    ScalarMap,
    SetupFieldDescriptor,
    SetupFieldType,
    SetupStep,
    TransportStatus,
    ValidationResult,
)
from domoai.runtime.connection_ports import (
    ConnectionProviderManifest,
    ConnectionProviderPort,
    ConnectionSetup,
)
from domoai.runtime.mqtt_transport import AiomqttTransport, MqttMessage, MqttTransport
from domoai.runtime.ports import AdapterPort
from domoai.security.secrets import SecretResolverPort


@dataclass(frozen=True, slots=True)
class BrokerTransportConfig:
    """Resolved broker configuration kept only at the transport boundary."""

    host: str
    port: int
    username: str | None
    password: str | None = field(default=None, repr=False)
    timeout: float = 5.0
    tls: bool = False
    ca_cert_path: Path | None = None
    client_cert_path: Path | None = None
    client_key_path: Path | None = None


BrokerDiscoveryPort = Callable[
    [], Awaitable[Sequence[ConnectionCandidate]] | Sequence[ConnectionCandidate]
]
TransportFactory = Callable[
    [BrokerTransportConfig], MqttTransport | Awaitable[MqttTransport]
]


class _SetupError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _BridgeObservation:
    base_topic: str | None = None
    bridge_state: str | None = None
    devices_seen: bool = False
    device_count: int | None = None
    evidence_seen: bool = False


class Zigbee2MqttConnectionProvider(ConnectionProviderPort):
    """Discover, validate and build one Zigbee2MQTT broker connection."""

    manifest = ConnectionProviderManifest(
        connector_id="zigbee2mqtt",
        display_name="Zigbee2MQTT",
        protocol="mqtt",
        version="1.0",
        steps=(
            SetupStep(
                step_id="broker",
                title="MQTT broker",
                input_keys=("url", "host", "port", "tls"),
                fields=(
                    SetupFieldDescriptor(
                        key="url", label="Broker URL", input_type=SetupFieldType.URL
                    ),
                    SetupFieldDescriptor(
                        key="host", label="Broker host", input_type=SetupFieldType.TEXT
                    ),
                    SetupFieldDescriptor(
                        key="port", label="Broker port", input_type=SetupFieldType.NUMBER
                    ),
                    SetupFieldDescriptor(
                        key="tls",
                        label="TLS",
                        input_type=SetupFieldType.SELECT,
                        options=("false", "true"),
                    ),
                ),
            ),
            SetupStep(
                step_id="credentials",
                title="MQTT credentials",
                input_keys=("username", "password"),
                secret_input_keys=("password",),
                fields=(
                    SetupFieldDescriptor(
                        key="username", label="Username", input_type=SetupFieldType.TEXT
                    ),
                    SetupFieldDescriptor(
                        key="password",
                        label="Password",
                        input_type=SetupFieldType.PASSWORD,
                        secret=True,
                    ),
                ),
            ),
            SetupStep(
                step_id="base_topic",
                title="Zigbee2MQTT base topic",
                input_keys=(
                    "base_topic",
                    "ca_cert_path",
                    "client_cert_path",
                    "client_key_path",
                ),
                fields=tuple(
                    SetupFieldDescriptor(
                        key=key,
                        label=key.replace("_", " ").title(),
                        input_type=SetupFieldType.TEXT,
                    )
                    for key in (
                        "base_topic",
                        "ca_cert_path",
                        "client_cert_path",
                        "client_key_path",
                    )
                ),
            ),
        ),
    )

    def __init__(
        self,
        *,
        secret_resolver: SecretResolverPort,
        transport_factory: TransportFactory | None = None,
        broker_discoverer: BrokerDiscoveryPort | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Zigbee2MQTT timeout must be positive")
        self.secret_resolver = secret_resolver
        self.transport_factory = transport_factory or _default_transport_factory
        self.broker_discoverer = broker_discoverer
        self.timeout_seconds = timeout_seconds

    async def discover(self) -> list[ConnectionCandidate]:
        if self.broker_discoverer is None:
            return []
        result = self.broker_discoverer()
        if inspect.isawaitable(result):
            result = await result
        candidates = list(result)
        if any(candidate.connector_id != self.manifest.connector_id for candidate in candidates):
            raise ValueError("Zigbee2MQTT discovery returned an incompatible candidate")
        return candidates

    def manual_candidate(
        self,
        url: str,
        *,
        base_topic: str | None = None,
    ) -> ConnectionCandidate:
        endpoint = _normalize_endpoint({"url": url})
        normalized_base_topic = _normalize_base_topic(base_topic)
        metadata: ScalarMap = {"source": "manual"}
        if normalized_base_topic is not None:
            metadata["base_topic"] = normalized_base_topic
        identity = endpoint["url"]
        if normalized_base_topic is not None:
            identity = f"{identity}/{normalized_base_topic}"
        return ConnectionCandidate(
            connector_id=self.manifest.connector_id,
            candidate_id=f"zigbee2mqtt:{identity}",
            display_name="Zigbee2MQTT",
            endpoint=endpoint,
            metadata=metadata,
        )

    async def probe(self, candidate: ConnectionCandidate) -> ProbeResult:
        try:
            config = self._config_from_endpoint(candidate.endpoint, {})
            base_topic = _candidate_base_topic(candidate)
            observation = await self._run_probe(config, base_topic)
            return _probe_result(observation)
        except Exception as error:  # noqa: BLE001 - boundary returns sanitized diagnostics
            return _failed_probe(_diagnostic_from_error(error))

    async def validate(self, setup: ConnectionSetup) -> ValidationResult:
        if setup.candidate.connector_id != self.manifest.connector_id:
            return ValidationResult(
                valid=False,
                diagnostics=(
                    _diagnostic(
                        "provider_mismatch",
                        "setup does not belong to Zigbee2MQTT",
                        retryable=False,
                    ),
                ),
            )
        try:
            config = self._config_from_setup(setup)
            base_topic = _option_base_topic(setup.draft.options)
            observation = await self._run_probe(config, base_topic)
            result = _probe_result(observation)
            if result.product_status is not ProductStatus.CONNECTED:
                return ValidationResult(valid=False, diagnostics=result.diagnostics)
            detected_topic = observation.base_topic or base_topic
            if detected_topic is None:
                return ValidationResult(
                    valid=False,
                    diagnostics=(
                        _diagnostic(
                            "zigbee2mqtt_not_detected",
                            "Zigbee2MQTT base topic was not detected",
                            retryable=False,
                        ),
                    ),
                )
            options = dict(setup.draft.options)
            options["base_topic"] = detected_topic
            normalized = setup.draft.model_copy(update={"options": options})
            return ValidationResult(valid=True, normalized_draft=normalized)
        except Exception as error:  # noqa: BLE001 - boundary returns sanitized diagnostics
            return ValidationResult(valid=False, diagnostics=(_diagnostic_from_error(error),))

    async def build_adapter(self, setup: ConnectionSetup, *, adapter_id: str) -> AdapterPort:
        validation = await self.validate(setup)
        if not validation.valid:
            raise ValueError("Zigbee2MQTT connection validation failed")
        normalized = validation.normalized_draft or setup.draft
        config = self._config_from_setup(
            setup.model_copy(update={"draft": normalized})
        )
        base_topic = _option_base_topic(normalized.options) or "zigbee2mqtt"
        transport = await _make_transport(self.transport_factory, config)
        return Zigbee2MqttAdapter(
            transport,
            adapter_id=adapter_id,
            base_topic=base_topic,
            discovery_timeout=config.timeout,
        )

    async def diagnose(self, setup: ConnectionSetup) -> tuple[ConnectionDiagnostic, ...]:
        return (await self.validate(setup)).diagnostics

    def _config_from_endpoint(
        self,
        endpoint: Mapping[str, object],
        options: Mapping[str, object],
    ) -> BrokerTransportConfig:
        normalized = _normalize_endpoint(endpoint)
        username = options.get("username")
        if username is not None and (not isinstance(username, str) or not username.strip()):
            raise _SetupError("invalid_credentials", "MQTT username is invalid")
        credential_ref = options.get("credential_ref")
        if credential_ref is not None:
            raise _SetupError("invalid_credentials", "MQTT credential reference is invalid")
        return _transport_config(
            normalized,
            options,
            username=username if isinstance(username, str) else None,
            password=None,
            default_timeout=self.timeout_seconds,
        )

    def _config_from_setup(self, setup: ConnectionSetup) -> BrokerTransportConfig:
        normalized = _normalize_endpoint(setup.draft.endpoint)
        username = setup.draft.options.get("username")
        if username is not None and (not isinstance(username, str) or not username.strip()):
            raise _SetupError("invalid_credentials", "MQTT username is invalid")
        password: str | None = None
        if setup.draft.credential_ref is not None:
            if username is None:
                raise _SetupError(
                    "invalid_credentials", "MQTT username is required for this credential"
                )
            try:
                password = self.secret_resolver.resolve(setup.draft.credential_ref)
            except Exception as error:  # noqa: BLE001 - secret errors are sanitized
                raise _SetupError(
                    "credential_unavailable", "MQTT credential is unavailable"
                ) from error
        return _transport_config(
            normalized,
            setup.draft.options,
            username=username if isinstance(username, str) else None,
            password=password,
            default_timeout=self.timeout_seconds,
        )

    async def _run_probe(
        self,
        config: BrokerTransportConfig,
        base_topic: str | None,
    ) -> _BridgeObservation:
        transport = await _make_transport(self.transport_factory, config)
        connected = False
        try:
            await transport.connect()
            connected = True
            await transport.subscribe(
                f"{base_topic}/bridge/#" if base_topic is not None else "+/bridge/#"
            )
            observation = _BridgeObservation(base_topic=base_topic)
            deadline = asyncio.get_running_loop().time() + config.timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                message = await transport.receive(remaining)
                if message is None:
                    break
                _ingest_bridge_message(message, observation, base_topic)
            return observation
        finally:
            if connected:
                try:
                    await transport.disconnect()
                except (ConnectionError, OSError, TimeoutError):
                    pass


def _default_transport_factory(config: BrokerTransportConfig) -> MqttTransport:
    return AiomqttTransport(
        config.host,
        port=config.port,
        username=config.username,
        password=config.password,
        timeout=config.timeout,
        tls=config.tls,
        ca_cert_path=config.ca_cert_path,
        client_cert_path=config.client_cert_path,
        client_key_path=config.client_key_path,
    )


async def _make_transport(
    factory: TransportFactory,
    config: BrokerTransportConfig,
) -> MqttTransport:
    result = factory(config)
    if inspect.isawaitable(result):
        return await result
    return result


def _normalize_endpoint(endpoint: Mapping[str, object]) -> ScalarMap:
    value = endpoint.get("url")
    if not isinstance(value, str) or not value.strip():
        raise _SetupError("invalid_endpoint", "MQTT broker URL is required")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"mqtt", "mqtts"}:
        raise _SetupError("invalid_endpoint", "MQTT broker URL scheme is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise _SetupError("invalid_endpoint", "MQTT broker URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise _SetupError("invalid_endpoint", "MQTT broker URL must not contain a path")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _SetupError("invalid_endpoint", "MQTT broker port is invalid") from None
    if host is None or not host.strip():
        raise _SetupError("invalid_endpoint", "MQTT broker host is required")
    normalized_host = host.strip().lower()
    if port is None:
        port = 8883 if parsed.scheme == "mqtts" else 1883
    if not 1 <= port <= 65535:
        raise _SetupError("invalid_endpoint", "MQTT broker port is invalid")
    url_host = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return {
        "url": f"{parsed.scheme}://{url_host}:{port}",
        "host": normalized_host,
        "port": port,
        "tls": parsed.scheme == "mqtts",
    }


def _transport_config(
    endpoint: ScalarMap,
    options: Mapping[str, object],
    *,
    username: str | None,
    password: str | None,
    default_timeout: float,
) -> BrokerTransportConfig:
    host = endpoint.get("host")
    port = endpoint.get("port")
    if not isinstance(host, str) or not host.strip():
        raise _SetupError("invalid_endpoint", "MQTT broker host is required")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise _SetupError("invalid_endpoint", "MQTT broker port is invalid")
    tls = endpoint.get("tls") is True
    if options.get("tls_insecure") is True:
        raise _SetupError(
            "tls_configuration_invalid",
            "insecure TLS is not allowed for new Zigbee2MQTT connections",
        )
    if username is not None and password is None:
        raise _SetupError("credential_required", "MQTT password is required for this username")
    if username is None and password is not None:
        raise _SetupError("invalid_credentials", "MQTT username is required for this password")
    timeout_value = options.get("timeout_seconds", default_timeout)
    if isinstance(timeout_value, bool) or not isinstance(timeout_value, (int, float)):
        raise _SetupError("invalid_timeout", "MQTT timeout is invalid")
    timeout = float(timeout_value)
    if timeout <= 0:
        raise _SetupError("invalid_timeout", "MQTT timeout must be positive")
    ca_cert_path = _path_option(options, "ca_cert_path")
    client_cert_path = _path_option(options, "client_cert_path")
    client_key_path = _path_option(options, "client_key_path")
    if (client_cert_path is None) != (client_key_path is None):
        raise _SetupError(
            "tls_configuration_invalid",
            "MQTT client certificate and key must be configured together",
        )
    return BrokerTransportConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        timeout=timeout,
        tls=tls,
        ca_cert_path=ca_cert_path,
        client_cert_path=client_cert_path,
        client_key_path=client_key_path,
    )


def _path_option(options: Mapping[str, object], key: str) -> Path | None:
    value = options.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _SetupError("tls_configuration_invalid", f"MQTT {key} is invalid")
    return Path(value)


def _normalize_base_topic(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _SetupError("invalid_base_topic", "Zigbee2MQTT base topic is invalid")
    normalized = value.strip().strip("/")
    if not normalized or "#" in normalized or "+" in normalized:
        raise _SetupError("invalid_base_topic", "Zigbee2MQTT base topic is invalid")
    return normalized


def _option_base_topic(options: Mapping[str, object]) -> str | None:
    return _normalize_base_topic(options.get("base_topic"))


def _candidate_base_topic(candidate: ConnectionCandidate) -> str | None:
    return _normalize_base_topic(candidate.metadata.get("base_topic"))


def _ingest_bridge_message(
    message: MqttMessage,
    observation: _BridgeObservation,
    expected_base_topic: str | None,
) -> None:
    marker = "/bridge/"
    if marker not in message.topic or not message.retained:
        return
    prefix, relative = message.topic.split(marker, 1)
    if expected_base_topic is not None and prefix != expected_base_topic:
        return
    try:
        payload: Any = json.loads(message.payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if relative == "state" and isinstance(payload, Mapping):
        state = payload.get("state")
        if isinstance(state, str) and state in {"online", "offline"}:
            observation.bridge_state = state
            observation.evidence_seen = True
            observation.base_topic = prefix
    elif relative == "info" and isinstance(payload, Mapping):
        observation.evidence_seen = True
        observation.base_topic = prefix
    elif relative == "devices" and isinstance(payload, list):
        observation.devices_seen = True
        observation.device_count = len(payload)
        observation.evidence_seen = True
        observation.base_topic = prefix


def _probe_result(observation: _BridgeObservation) -> ProbeResult:
    if not observation.evidence_seen:
        return _failed_probe(
            _diagnostic(
                "zigbee2mqtt_not_detected",
                "Zigbee2MQTT bridge was not detected",
                retryable=False,
            ),
            transport_status=TransportStatus.REACHABLE,
        )
    if observation.bridge_state == "offline":
        return _failed_probe(
            _diagnostic(
                "bridge_offline",
                "Zigbee2MQTT bridge is offline",
                retryable=True,
                severity=DiagnosticSeverity.WARNING,
            ),
            transport_status=TransportStatus.REACHABLE,
            product_status=ProductStatus.DEGRADED,
        )
    if observation.bridge_state != "online":
        return _failed_probe(
            _diagnostic(
                "zigbee2mqtt_not_detected",
                "Zigbee2MQTT bridge is not online",
                retryable=True,
            ),
            transport_status=TransportStatus.REACHABLE,
        )
    return ProbeResult(
        transport_status=TransportStatus.REACHABLE,
        inventory_status=InventoryStatus.READY
        if observation.device_count is not None and observation.device_count > 0
        else InventoryStatus.EMPTY,
        product_status=ProductStatus.CONNECTED,
    )


def _failed_probe(
    diagnostic: ConnectionDiagnostic,
    *,
    transport_status: TransportStatus = TransportStatus.UNREACHABLE,
    product_status: ProductStatus = ProductStatus.UNAVAILABLE,
) -> ProbeResult:
    return ProbeResult(
        transport_status=transport_status,
        inventory_status=InventoryStatus.FAILED,
        product_status=product_status,
        diagnostics=(diagnostic,),
    )


def _diagnostic(
    code: str,
    message: str,
    *,
    retryable: bool,
    severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
) -> ConnectionDiagnostic:
    return ConnectionDiagnostic(
        code=code,
        severity=severity,
        retryable=retryable,
        message=message,
    )


def _diagnostic_from_error(error: Exception) -> ConnectionDiagnostic:
    if isinstance(error, _SetupError):
        return _diagnostic(error.code, str(error), retryable=False)
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return _diagnostic(
            "tls_certificate_invalid",
            "MQTT TLS certificate validation failed",
            retryable=False,
        )
    if any(isinstance(item, PermissionError) for item in chain):
        return _diagnostic(
            "authentication_failed",
            "MQTT broker authentication failed",
            retryable=False,
        )
    if any(isinstance(item, (TimeoutError, asyncio.TimeoutError)) for item in chain):
        return _diagnostic("timeout", "MQTT broker probe timed out", retryable=True)
    return _diagnostic("unreachable", "MQTT broker is unavailable", retryable=True)


__all__ = [
    "BrokerDiscoveryPort",
    "BrokerTransportConfig",
    "TransportFactory",
    "Zigbee2MqttConnectionProvider",
]
