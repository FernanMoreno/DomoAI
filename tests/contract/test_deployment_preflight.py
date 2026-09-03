from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from domoai.admin.cli import main
from domoai.admin.deployment_preflight import (
    DeploymentPreflightRequest,
    _validate_compose,
    _validate_proxy,
    run_preflight,
)


def _write_deployment(
    tmp_path: Path, *, public_url: str = "https://mcp.example.test"
) -> tuple[DeploymentPreflightRequest, str]:
    secret = "fixture-bearer-not-for-output"
    clients = tmp_path / "clients.json"
    clients.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "codex",
                        "token_hash": hashlib.sha256(secret.encode()).hexdigest(),
                        "scopes": ["mcp:read", "mcp:write"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    knx = tmp_path / "knx.json"
    knx.write_text("{}", encoding="utf-8")
    env = tmp_path / "gateway.env"
    env.write_text(
        "\n".join(
            [
                "DOMOAI_MCP_HOST=0.0.0.0",
                "DOMOAI_MCP_PORT=8124",
                "DOMOAI_MCP_PATH=/mcp",
                f"DOMOAI_MCP_PUBLIC_URL={public_url}",
                "DOMOAI_CADDY_HOSTNAME=mcp.example.test",
                "DOMOAI_MCP_CLIENT_TOKEN_FILE=/run/secrets/mcp-clients.json",
                "DOMOAI_MCP_DEPLOYMENT_ID=home-main",
                "DOMOAI_HOME_ASSISTANT_URL=http://homeassistant:8123",
                "DOMOAI_HOME_ASSISTANT_TOKEN=fixture-ha-secret",
                "DOMOAI_ZIGBEE2MQTT_URL=mqtt://mqtt:1883",
                "DOMOAI_KNX_GATEWAY_HOST=host.docker.internal",
                "DOMOAI_KNX_GATEWAY_PORT=3672",
                "DOMOAI_KNX_CONFIG_PATH=/app/config/knx.json",
                f"DOMOAI_KNX_CONFIG_PATH_HOST={knx}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        """
services:
  mqtt:
    image: eclipse-mosquitto:2
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
  gateway:
    expose: [8124]
    volumes:
      - domoai-data:/app/data
      - ./clients.json:/run/secrets/mcp-clients.json:ro
      - ./knx.json:/app/config/knx-config.json:ro
  proxy:
    image: caddy:2.11.4-alpine
    ports:
      - 0.0.0.0:80:80
      - 0.0.0.0:443:443
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
volumes:
  domoai-data:
""".strip()
        + "\n",
        encoding="utf-8",
    )
    caddy = tmp_path / "Caddyfile"
    caddy.write_text(
        """
{$DOMOAI_CADDY_HOSTNAME} {
    tls internal
    @mcp path /mcp /mcp/*
    handle @mcp {
        reverse_proxy gateway:8124
    }
    @health path /healthz /readyz
    handle @health {
        reverse_proxy gateway:8124
    }
    handle {
        respond 404
    }
}
""".lstrip(),
        encoding="utf-8",
    )
    return (
        DeploymentPreflightRequest(
            env_file=env,
            clients_file=clients,
            compose_file=compose,
            caddyfile=caddy,
        ),
        secret,
    )


def test_preflight_understands_the_checked_in_deployment_boundary() -> None:
    root = Path(__file__).parents[2]
    compose = (root / "deploy/compose.yaml").read_text(encoding="utf-8")
    caddy = (root / "deploy/reverse-proxy/Caddyfile").read_text(encoding="utf-8")

    assert _validate_compose(compose)
    assert _validate_proxy(caddy, {"DOMOAI_CADDY_HOSTNAME": "mcp.example.test"})


def test_preflight_rejects_gateway_port_mismatch(tmp_path: Path) -> None:
    request, _ = _write_deployment(tmp_path)
    request.env_file.write_text(
        request.env_file.read_text(encoding="utf-8").replace(
            "DOMOAI_MCP_PORT=8124", "DOMOAI_MCP_PORT=9999"
        ),
        encoding="utf-8",
    )

    report = asyncio.run(run_preflight(request))

    assert report.status == "failed"
    assert any(check.code == "compose_boundary_invalid" for check in report.checks)


def test_preflight_rejects_public_mcp_path_not_exposed_by_proxy(tmp_path: Path) -> None:
    request, _ = _write_deployment(tmp_path)
    request.env_file.write_text(
        request.env_file.read_text(encoding="utf-8").replace(
            "DOMOAI_MCP_PATH=/mcp", "DOMOAI_MCP_PATH=/other"
        ),
        encoding="utf-8",
    )

    report = asyncio.run(run_preflight(request))

    assert report.status == "failed"
    assert any(check.code == "proxy_boundary_invalid" for check in report.checks)


def test_preflight_rejects_client_file_not_used_by_the_gateway_mount(tmp_path: Path) -> None:
    request, _ = _write_deployment(tmp_path)
    other_clients = tmp_path / "other-clients.json"
    other_clients.write_text(request.clients_file.read_text(encoding="utf-8"), encoding="utf-8")
    request.compose_file.write_text(
        request.compose_file.read_text(encoding="utf-8").replace(
            "./clients.json:/run/secrets/mcp-clients.json:ro",
            "./other-clients.json:/run/secrets/mcp-clients.json:ro",
        ),
        encoding="utf-8",
    )

    report = asyncio.run(run_preflight(request))

    assert report.status == "failed"
    assert any(check.code == "referenced_file_unavailable" for check in report.checks)


def test_preflight_accepts_a_trusted_tls_directive() -> None:
    root = Path(__file__).parents[2]
    caddy = (root / "deploy/reverse-proxy/Caddyfile").read_text(encoding="utf-8")

    assert _validate_proxy(
        caddy.replace("tls internal", "tls /etc/caddy/cert.pem /etc/caddy/key.pem"),
        {"DOMOAI_CADDY_HOSTNAME": "mcp.example.test"},
    )


def test_preflight_passes_valid_deployment_without_leaking_secrets_or_paths(tmp_path: Path) -> None:
    request, secret = _write_deployment(tmp_path)

    report = asyncio.run(run_preflight(request))
    encoded = json.dumps(report.to_dict(), sort_keys=True)

    assert report.status == "passed"
    assert report.deployment_id == "home-main"
    assert {check.code for check in report.checks} >= {
        "environment_valid",
        "clients_file_valid",
        "clients_usable",
        "compose_boundary_valid",
        "proxy_boundary_valid",
        "referenced_files_valid",
    }
    assert secret not in encoded
    assert str(tmp_path) not in encoded


def test_preflight_rejects_non_loopback_http_before_network_checks(tmp_path: Path) -> None:
    request, _ = _write_deployment(tmp_path, public_url="http://mcp.example.test")
    request = replace(request, network=True)

    report = asyncio.run(run_preflight(request))

    assert report.status == "failed"
    assert any(check.code == "environment_invalid" for check in report.checks)
    assert not any(check.name == "network_dependencies" for check in report.checks)


def test_preflight_rejects_invalid_client_file_with_sanitized_report(tmp_path: Path) -> None:
    request, secret = _write_deployment(tmp_path)
    request.clients_file.write_text(
        json.dumps({"clients": [{"client_id": "bad", "token_hash": secret}]}),
        encoding="utf-8",
    )

    report = asyncio.run(run_preflight(request))
    encoded = json.dumps(report.to_dict(), sort_keys=True)

    assert report.status == "failed"
    assert any(check.code == "clients_file_invalid" for check in report.checks)
    assert secret not in encoded
    assert str(tmp_path) not in encoded


def test_admin_cli_returns_machine_readable_preflight_result(tmp_path: Path, capsys) -> None:
    request, _ = _write_deployment(tmp_path)
    exit_code = main(
        [
            "deployment",
            "preflight",
            "--env-file",
            str(request.env_file),
            "--clients-file",
            str(request.clients_file),
            "--compose-file",
            str(request.compose_file),
            "--caddyfile",
            str(request.caddyfile),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["schema_version"] == "v1"
    assert output["status"] == "passed"
