from __future__ import annotations

import json
from pathlib import Path

import pytest

from domoai.adapters.knx.connection import KnxConnectionProvider
from domoai.adapters.knx.transport import KnxSecureTransportConfig
from domoai.domain.connections import ConnectionCandidate, ConnectionDraft
from domoai.runtime.connection_ports import ConnectionSetup


def _setup(tmp_path: Path, *, mode: str = "ip_and_data_secure") -> ConnectionSetup:
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "entities": [
                    {
                        "entity_id": "secure-light",
                        "name": "Secure Light",
                        "area_id": "living",
                        "semantic_type": "light",
                        "capabilities": [
                            {
                                "name": "power",
                                "dpt": "1.001",
                                "state_group_address": "1/0/1",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate = ConnectionCandidate(
        connector_id="knx",
        candidate_id="knx:secure:3671",
        display_name="Secure KNX",
        endpoint={"host": "192.0.2.10", "port": 3671},
    )
    draft = ConnectionDraft(
        connector_id="knx",
        unique_id=candidate.candidate_id,
        endpoint=candidate.endpoint,
        options={
            "secure_mode": mode,
            "secure_keyring_path": str(tmp_path / "house.knxkeys"),
            "secure_user_id": 7,
        },
        credential_ref="secret:12345678-1234-1234-1234-123456789012",
        mapping_ref=str(mapping),
    )
    return ConnectionSetup(candidate=candidate, draft=draft)


class _Resolver:
    def resolve(self, _reference: str) -> str:
        return json.dumps(
            {
                "keyring_password": "keyring-password",
                "user_password": "user-password",
                "device_authentication_password": "device-password",
                "backbone_key": "00" * 16,
            }
        )


class _Transport:
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def read_group(self, group_address: str, dpt: str): ...

    async def write_group(self, group_address: str, dpt: str, value, *, execution_context=None): ...

    async def receive(self, timeout=None): ...

    async def health(self) -> bool:
        return True

    def set_group_read_response(self, group_address: str, dpt: str, value) -> None: ...


@pytest.mark.asyncio
async def test_secure_provider_builds_explicit_transport_configuration(tmp_path: Path) -> None:
    captured: list[KnxSecureTransportConfig] = []

    def factory(host: str, port: int, route_back: bool, dpts: dict[str, str], secure) -> _Transport:
        del host, port, route_back, dpts
        captured.append(secure)
        return _Transport()

    provider = KnxConnectionProvider(
        secret_resolver=_Resolver(), secure_transport_factory=factory
    )
    setup = _setup(tmp_path)

    result = await provider.validate(setup)
    adapter = await provider.build_adapter(setup, adapter_id="knx-secure-1")

    assert result.valid is True
    assert captured[0].mode == "ip_and_data_secure"
    assert captured[0].user_password == "user-password"
    assert captured[0].keyring_password == "keyring-password"
    assert adapter.adapter_id == "knx-secure-1"


@pytest.mark.asyncio
async def test_secure_provider_rejects_missing_secret_resolver(tmp_path: Path) -> None:
    provider = KnxConnectionProvider()
    result = await provider.validate(_setup(tmp_path))

    assert result.valid is False
    assert result.diagnostics[0].code == "knx_secure_credentials_required"


def test_secure_transport_config_never_exposes_repr_secrets() -> None:
    config = KnxSecureTransportConfig(
        mode="ip_secure",
        keyring_path=None,
        keyring_password="secret-keyring",
        user_id=7,
        user_password="secret-user",
        device_authentication_password="secret-device",
        backbone_key=None,
    )

    assert "secret-keyring" not in repr(config)
    assert "secret-user" not in repr(config)
