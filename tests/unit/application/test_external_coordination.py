from pathlib import Path

import pytest

from domoai.application.etcd_coordination import build_external_lease_coordinator
from domoai.config.settings import Settings


def test_external_coordinator_requires_endpoints_and_all_mtls_files(tmp_path: Path) -> None:
    settings = Settings(multi_host_enabled=True)

    with pytest.raises(ValueError, match="etcd endpoints"):
        build_external_lease_coordinator(settings)

    settings = settings.model_copy(update={"etcd_endpoints": ("https://etcd.test:2379",)})
    with pytest.raises(ValueError, match="mTLS"):
        build_external_lease_coordinator(settings)


def test_external_coordinator_rejects_plaintext_etcd(tmp_path: Path) -> None:
    settings = Settings(
        multi_host_enabled=True,
        etcd_endpoints=("http://etcd.test:2379",),
        etcd_ca_cert_path=tmp_path / "ca.pem",
        etcd_client_cert_path=tmp_path / "client.pem",
        etcd_client_key_path=tmp_path / "client.key",
    )

    with pytest.raises(ValueError, match="HTTPS"):
        build_external_lease_coordinator(settings)
