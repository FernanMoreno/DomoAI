import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from domoai.admin import cli
from domoai.config.settings import Settings
from domoai.domain.coordination import LeaseScope
from domoai.domain.multihost_qualification import (
    REQUIRED_MULTIHOST_CHECKS,
    MultiHostQualificationCheck,
    MultiHostQualificationEvidence,
)


def _evidence(scope: LeaseScope, gateway_identity: str) -> MultiHostQualificationEvidence:
    completed_at = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return MultiHostQualificationEvidence(
        scope=scope,
        gateway_identity=gateway_identity,
        completed_at=completed_at,
        expires_at=completed_at + timedelta(days=1),
        checks=[
            MultiHostQualificationCheck(check_id=check_id, status="passed")
            for check_id in sorted(REQUIRED_MULTIHOST_CHECKS)
        ],
    )


def test_parser_requires_explicit_physical_fencing_confirmation() -> None:
    args = cli._parser().parse_args(
        [
            "qualify-multihost",
            "--gateway-command",
            "gateway-bridge",
            "--safe-command",
            "operator-safe-noop",
            "--output",
            "qualification.json",
        ]
    )

    assert args.command == "qualify-multihost"
    assert args.confirm_physical_fencing is False


def test_command_refuses_physical_probe_without_confirmation(tmp_path: Path, capsys) -> None:
    exit_code = cli.main(
        [
            "qualify-multihost",
            "--gateway-command",
            "gateway-bridge",
            "--safe-command",
            "operator-safe-noop",
            "--output",
            str(tmp_path / "qualification.json"),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "error": {"code": "multihost_physical_probe_confirmation_required"}
    }


def test_command_writes_sanitized_evidence_after_attended_run(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    certificate_paths = tuple(tmp_path / name for name in ("ca.pem", "cert.pem", "key.pem"))
    for path in certificate_paths:
        path.write_text("fixture", encoding="utf-8")
    settings = Settings(
        multi_host_enabled=True,
        etcd_endpoints=("https://etcd-1.test", "https://etcd-2.test", "https://etcd-3.test"),
        etcd_ca_cert_path=certificate_paths[0],
        etcd_client_cert_path=certificate_paths[1],
        etcd_client_key_path=certificate_paths[2],
        postgres_dsn=SecretStr("postgresql://user:secret@postgres.test/domoai"),
        postgres_sslrootcert=certificate_paths[0],
        postgres_sslcert=certificate_paths[1],
        postgres_sslkey=certificate_paths[2],
        multi_host_gateway_identity="gateway-serial-1",
        mcp_tenant_id="tenant",
        mcp_household_id="home",
        mcp_deployment_id="edge",
    )
    scope = LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge")

    class Runner:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        async def run(self, **kwargs) -> MultiHostQualificationEvidence:
            assert kwargs["scope"] == scope
            assert kwargs["safe_command"] == "operator-safe-noop"
            return _evidence(scope, "gateway-serial-1")

    class ClosingClient:
        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(cli.Settings, "from_environment", lambda: settings)
    monkeypatch.setattr(cli, "build_external_lease_coordinator", lambda _: object())
    monkeypatch.setattr(cli, "build_external_etcd_http_client", lambda _: ClosingClient())
    monkeypatch.setattr(cli, "MultiHostQualificationRunner", Runner)
    output_path = tmp_path / "qualification.json"

    exit_code = cli.main(
        [
            "qualify-multihost",
            "--confirm-physical-fencing",
            "--gateway-command",
            "gateway-bridge",
            "--safe-command",
            "operator-safe-noop",
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "passed"
    assert payload["evidence_digest"].startswith("sha256:")
    assert "secret" not in output_path.read_text(encoding="utf-8")


def test_command_closes_quorum_client_when_coordinator_cannot_be_built(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    certificate_paths = tuple(tmp_path / name for name in ("ca.pem", "cert.pem", "key.pem"))
    for path in certificate_paths:
        path.write_text("fixture", encoding="utf-8")
    settings = Settings(
        multi_host_enabled=True,
        etcd_endpoints=("https://etcd-1.test", "https://etcd-2.test", "https://etcd-3.test"),
        etcd_ca_cert_path=certificate_paths[0],
        etcd_client_cert_path=certificate_paths[1],
        etcd_client_key_path=certificate_paths[2],
        postgres_dsn=SecretStr("postgresql://user:secret@postgres.test/domoai"),
        postgres_sslrootcert=certificate_paths[0],
        postgres_sslcert=certificate_paths[1],
        postgres_sslkey=certificate_paths[2],
        multi_host_gateway_identity="gateway-serial-1",
    )

    class ClosingClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    client = ClosingClient()
    monkeypatch.setattr(cli.Settings, "from_environment", lambda: settings)
    monkeypatch.setattr(cli, "build_external_etcd_http_client", lambda _: client)
    monkeypatch.setattr(
        cli,
        "build_external_lease_coordinator",
        lambda _: (_ for _ in ()).throw(ValueError("invalid mTLS")),
    )

    exit_code = cli.main(
        [
            "qualify-multihost",
            "--confirm-physical-fencing",
            "--gateway-command",
            "gateway-bridge",
            "--safe-command",
            "operator-safe-noop",
            "--output",
            str(tmp_path / "qualification.json"),
        ]
    )

    assert exit_code == 2
    assert client.closed is True
    assert json.loads(capsys.readouterr().out) == {
        "error": {"code": "multihost_qualification_coordinator_unavailable"}
    }
