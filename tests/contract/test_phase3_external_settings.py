from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.config.settings import Settings


def test_external_coordination_settings_are_secret_safe_and_bounded(tmp_path: Path) -> None:
    settings = Settings(
        multi_host_enabled=True,
        instance_id="host-a",
        etcd_endpoints=("https://etcd-1.internal:2379", "https://etcd-2.internal:2379"),
        etcd_ca_cert_path=tmp_path / "etcd-ca.pem",
        etcd_client_cert_path=tmp_path / "etcd-client.pem",
        etcd_client_key_path=tmp_path / "etcd-client.key",
        postgres_dsn=SecretStr("postgresql://user:secret@db.internal/domoai"),
        postgres_sslrootcert=tmp_path / "postgres-ca.pem",
        postgres_sslcert=tmp_path / "postgres-client.pem",
        postgres_sslkey=tmp_path / "postgres-client.key",
    )

    assert settings.etcd_endpoints[0].startswith("https://")
    assert settings.postgres_dsn is not None
    assert "secret" not in repr(settings)


def test_active_active_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="active-active"):
        Settings(active_active_enabled=True)
