from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _compose() -> dict[str, object]:
    parsed = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_production_proxy_requires_operator_supplied_tls_material() -> None:
    caddyfile = (ROOT / "deploy/reverse-proxy/Caddyfile").read_text(encoding="utf-8")
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    proxy = services["proxy"]
    assert isinstance(proxy, dict)

    assert "tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem" in caddyfile
    assert "tls internal" not in caddyfile
    proxy_mounts = proxy["volumes"]
    assert (
        "${DOMOAI_TLS_CERT_FILE_HOST:-./tls/fullchain.pem}:/etc/caddy/certs/fullchain.pem:ro"
        in proxy_mounts
    )
    assert (
        "${DOMOAI_TLS_KEY_FILE_HOST:-./tls/privkey.pem}:/etc/caddy/certs/privkey.pem:ro"
        in proxy_mounts
    )


def test_lab_internal_ca_is_separate_and_not_the_production_mount() -> None:
    production = (ROOT / "deploy/reverse-proxy/Caddyfile").read_text(encoding="utf-8")
    lab = (ROOT / "deploy/reverse-proxy/Caddyfile.internal").read_text(encoding="utf-8")
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    proxy = services["proxy"]
    assert isinstance(proxy, dict)

    assert "tls internal" in lab
    assert "tls internal" not in production
    assert "Caddyfile.internal" not in "\n".join(str(item) for item in proxy["volumes"])


def test_proxy_host_exposure_is_explicit() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    proxy = services["proxy"]
    assert isinstance(proxy, dict)

    assert proxy["ports"] == [
        "${DOMOAI_PROXY_HTTP_BIND:-127.0.0.1}:80:80",
        "${DOMOAI_PROXY_HTTPS_BIND:-127.0.0.1}:443:443",
    ]

    environment = (ROOT / "deploy/gateway.env.example").read_text(encoding="utf-8")
    assert "DOMOAI_TLS_CERT_FILE_HOST" in environment
    assert "DOMOAI_TLS_KEY_FILE_HOST" in environment
    assert "DOMOAI_PROXY_HTTPS_BIND=192.0.2.10" in environment
