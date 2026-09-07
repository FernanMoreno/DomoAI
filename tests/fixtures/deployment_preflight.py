from __future__ import annotations

import hashlib
import json
from pathlib import Path

from domoai.admin.deployment_preflight import DeploymentPreflightRequest


def write_deployment(
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
