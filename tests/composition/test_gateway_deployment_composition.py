from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_complete_stack_lifecycle_and_durable_state_contract() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    volumes = compose["volumes"]

    assert set(services) == {"mqtt", "homeassistant", "gateway", "proxy"}
    assert set(volumes) >= {
        "domoai-data",
        "homeassistant-config",
        "mqtt-data",
        "caddy-data",
        "caddy-config",
    }

    for service_name in services:
        service = services[service_name]
        assert service["restart"] == "unless-stopped"
        # Home Assistant's s6-overlay must remain PID 1; Docker's init shim
        # makes it exit with code 100 ("can only run as pid 1").
        if service_name == "homeassistant":
            assert "init" not in service
        else:
            assert service["init"] is True
        if service_name != "gateway":
            assert service["stop_grace_period"] == "30s"

    assert services["gateway"]["stop_grace_period"] == "45s"
    assert services["gateway"]["depends_on"]["mqtt"]["condition"] == "service_healthy"
    assert (
        services["gateway"]["depends_on"]["homeassistant"]["condition"]
        == "service_healthy"
    )
    assert services["gateway"]["healthcheck"]["test"][0] == "CMD-SHELL"
    assert services["proxy"]["healthcheck"]["test"][0] == "CMD-SHELL"

    gateway_mounts = services["gateway"]["volumes"]
    assert any(str(mount).startswith("domoai-data:") for mount in gateway_mounts)
    assert any(
        str(mount).endswith(":/run/secrets/mcp-clients.json:ro")
        for mount in gateway_mounts
    )
    assert any(
        str(mount).endswith(":/app/config/knx-config.json:ro")
        for mount in gateway_mounts
    )

    proxy_mounts = services["proxy"]["volumes"]
    assert "./reverse-proxy/Caddyfile:/etc/caddy/Caddyfile:ro" in proxy_mounts
    assert "caddy-data:/data" in proxy_mounts
    assert "caddy-config:/config" in proxy_mounts


def test_gateway_and_proxy_health_contracts_do_not_confuse_liveness_and_readiness() -> None:
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    gateway_health = " ".join(services["gateway"]["healthcheck"]["test"])
    proxy_health = " ".join(services["proxy"]["healthcheck"]["test"])

    assert "/healthz" in gateway_health
    assert "/healthz" in proxy_health
    assert "curl" in proxy_health
    assert "wget" not in proxy_health
    assert "/readyz" not in proxy_health
    assert "127.0.0.1" in proxy_health
    assert "--resolve" in proxy_health
    assert "$${DOMOAI_CADDY_HOSTNAME}" in proxy_health
