import datetime
import ssl
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from domoai.adapters.zigbee2mqtt.transport import AiomqttTransport, MqttMessage


def _write_self_signed_cert_and_key(directory: Path, common_name: str) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = directory / f"{common_name}.crt"
    key_path = directory / f"{common_name}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def test_tls_disabled_by_default_builds_no_context() -> None:
    transport = AiomqttTransport("broker.test")

    assert transport.tls is False


def test_tls_enabled_with_no_ca_or_cert_verifies_by_default() -> None:
    transport = AiomqttTransport("broker.test", tls=True)

    context = transport._build_tls_context()

    assert context.verify_mode != ssl.CERT_NONE
    assert context.check_hostname is True


def test_tls_context_loads_custom_ca(tmp_path: Path) -> None:
    ca_cert_path, _ = _write_self_signed_cert_and_key(tmp_path, "test-ca")
    transport = AiomqttTransport("broker.test", tls=True, ca_cert_path=ca_cert_path)

    context = transport._build_tls_context()

    assert context.cert_store_stats()["x509_ca"] >= 1


def test_tls_context_loads_client_cert_chain(tmp_path: Path) -> None:
    cert_path, key_path = _write_self_signed_cert_and_key(tmp_path, "test-client")
    transport = AiomqttTransport(
        "broker.test",
        tls=True,
        client_cert_path=cert_path,
        client_key_path=key_path,
    )

    context = transport._build_tls_context()

    assert context is not None


def test_tls_insecure_disables_verification() -> None:
    transport = AiomqttTransport("broker.test", tls=True, tls_insecure=True)

    context = transport._build_tls_context()

    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_tls_insecure_is_off_by_default() -> None:
    transport = AiomqttTransport("broker.test", tls=True)

    context = transport._build_tls_context()

    assert context.check_hostname is True
    assert context.verify_mode != ssl.CERT_NONE


def test_tls_insecure_overrides_configured_ca(tmp_path: Path) -> None:
    ca_cert_path, _ = _write_self_signed_cert_and_key(tmp_path, "test-ca")
    transport = AiomqttTransport(
        "broker.test",
        tls=True,
        ca_cert_path=ca_cert_path,
        tls_insecure=True,
    )

    context = transport._build_tls_context()

    assert context.cert_store_stats()["x509_ca"] >= 1
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


class _FakeAiomqttClient:
    captured_kwargs: dict[str, object] = {}

    def __init__(self, **kwargs: object) -> None:
        _FakeAiomqttClient.captured_kwargs = kwargs
        self.messages = _FakeMessages()

    async def __aenter__(self) -> "_FakeAiomqttClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _FakeMessages:
    def __aiter__(self) -> "_FakeMessages":
        return self


class _DisconnectedMessages:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def __aiter__(self) -> "_DisconnectedMessages":
        return self

    async def __anext__(self) -> MqttMessage:
        raise self.error


@pytest.mark.asyncio
async def test_plaintext_connect_passes_no_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import aiomqtt

    monkeypatch.setattr(aiomqtt, "Client", _FakeAiomqttClient)
    transport = AiomqttTransport("broker.test")

    await transport.connect()

    assert _FakeAiomqttClient.captured_kwargs["tls_context"] is None


@pytest.mark.asyncio
async def test_mqtts_connect_passes_a_tls_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import aiomqtt

    monkeypatch.setattr(aiomqtt, "Client", _FakeAiomqttClient)
    transport = AiomqttTransport("broker.test", tls=True)

    await transport.connect()

    assert isinstance(_FakeAiomqttClient.captured_kwargs["tls_context"], ssl.SSLContext)


@pytest.mark.asyncio
async def test_receive_converts_aiomqtt_disconnect_to_connection_error() -> None:
    import aiomqtt

    transport = AiomqttTransport("broker.test")
    transport._messages = _DisconnectedMessages(aiomqtt.MqttError("broker disconnected"))

    with pytest.raises(ConnectionError, match="MQTT receive failed"):
        await transport.receive()
