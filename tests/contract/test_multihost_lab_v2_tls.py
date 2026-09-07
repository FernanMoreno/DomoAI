from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TLS_DIR = ROOT / "deploy" / "multihost" / "qualification" / "tls"
GENERATOR = TLS_DIR / "generate.sh"
PROBE = ROOT / "deploy" / "multihost" / "qualification" / "tls_probe.py"
SECURE_COMPOSE = ROOT / "deploy" / "multihost" / "qualification" / "compose.tls.yaml"


def test_tls_profile_is_ephemeral_and_does_not_read_production_secrets() -> None:
    script = GENERATOR.read_text(encoding="utf-8")
    config = (TLS_DIR / "openssl.cnf").read_text(encoding="utf-8")

    assert "umask 077" in script
    assert "chmod 700" in script
    assert "BASH_SOURCE" in script
    assert "/run/secrets" not in script
    assert "v3_ca" in config
    assert "extendedKeyUsage = clientAuth" in config


def test_tls_probe_covers_rotation_invalid_and_expired_clients_without_pem_output() -> None:
    probe = PROBE.read_text(encoding="utf-8")
    compose = SECURE_COMPOSE.read_text(encoding="utf-8")

    for name in ("client.pem", "rotated-client.pem", "untrusted-client.pem", "expired-client.pem"):
        assert name in probe or name in GENERATOR.read_text(encoding="utf-8")
    assert "status" in probe
    assert 'print(json.dumps' in probe
    assert "pem" not in compose.lower()
    assert "ephemeral-mtls" in compose
