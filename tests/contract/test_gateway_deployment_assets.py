from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _compose() -> dict[str, object]:
    parsed = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def test_gateway_deployment_is_available_outside_the_lab_tree() -> None:
    expected = {
        "deploy/README.md",
        "deploy/Dockerfile",
        "deploy/compose.yaml",
        "deploy/gateway.env.example",
        "deploy/wsl/run-gateway.sh",
        "deploy/windows/run-gateway.ps1",
        "deploy/reverse-proxy/Caddyfile",
        ".dockerignore",
        "docs/unified-mcp.md",
    }

    assert all((ROOT / path).is_file() for path in expected)
    assert not any(path.startswith("dev/lab/") for path in expected)


def test_container_gateway_uses_distinct_gateway_and_protocol_coordinates() -> None:
    compose_text = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    environment = (ROOT / "deploy/gateway.env.example").read_text(encoding="utf-8")

    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)
    gateway = services["gateway"]
    assert isinstance(gateway, dict)

    assert "domoai-mcp-gateway" in compose_text
    assert "ports" not in gateway
    assert "8124" in gateway["expose"]
    assert "DOMOAI_MCP_PORT=8124" in environment
    assert "DOMOAI_KNX_GATEWAY_PORT=3672" in environment
    assert "host.docker.internal" in environment


def test_default_compose_has_one_edge_and_no_gateway_host_bypass() -> None:
    compose = _compose()
    services = compose["services"]
    assert isinstance(services, dict)

    proxy = services["proxy"]
    gateway = services["gateway"]
    assert isinstance(proxy, dict)
    assert isinstance(gateway, dict)

    assert proxy["image"].startswith("caddy:")
    assert proxy["ports"] == [
        "${DOMOAI_PROXY_HTTP_BIND:-0.0.0.0}:80:80",
        "${DOMOAI_PROXY_HTTPS_BIND:-0.0.0.0}:443:443",
    ]
    assert gateway.get("ports") is None
    assert gateway["expose"] == ["8124"]
    assert gateway["depends_on"]["mqtt"]["condition"] == "service_healthy"
    assert gateway["depends_on"]["homeassistant"]["condition"] == "service_healthy"
    assert services["mqtt"]["ports"] == [
        "127.0.0.1:${DOMOAI_MQTT_HOST_PORT:-1883}:1883"
    ]
    assert services["homeassistant"]["ports"] == [
        "127.0.0.1:${DOMOAI_HOME_ASSISTANT_HOST_PORT:-8123}:8123"
    ]


def test_caddy_route_contract_is_allowlisted() -> None:
    caddyfile = (ROOT / "deploy/reverse-proxy/Caddyfile").read_text(encoding="utf-8")

    assert "{$DOMOAI_CADDY_HOSTNAME}" in caddyfile
    assert "${DOMOAI_CADDY_HOSTNAME}" not in caddyfile
    assert "tls internal" in caddyfile
    assert "path /mcp /mcp/*" in caddyfile
    assert "path /healthz /readyz" in caddyfile
    assert caddyfile.count("reverse_proxy gateway:8124") == 2
    assert "handle {" in caddyfile
    assert "respond 404" in caddyfile
    assert "reverse_proxy gateway:8124" not in caddyfile.split("handle {", 1)[-1]


def test_container_context_excludes_lab_and_runtime_secrets() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "dev/lab/" in dockerignore
    assert ".env*" in dockerignore
    assert "*.sqlite3" in dockerignore


def test_wsl_launcher_does_not_leave_signal_handling_to_uv_wrapper() -> None:
    launcher = (ROOT / "deploy/wsl/run-gateway.sh").read_text(encoding="utf-8")

    assert 'GATEWAY_BIN="$PROJECT_ROOT/.venv/bin/domoai-mcp-gateway"' in launcher
    assert 'exec "$GATEWAY_BIN"' in launcher
