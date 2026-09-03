from __future__ import annotations

from pathlib import Path

import pytest

from domoai.config.settings import Settings


@pytest.mark.parametrize(
    ("name", "host", "public_url"),
    [
        ("native", "127.0.0.1", "http://127.0.0.1:8124"),
        ("wsl", "0.0.0.0", "https://mcp.wsl.example"),
        ("docker", "0.0.0.0", "https://mcp.docker.example"),
        ("windows", "0.0.0.0", "https://mcp.windows.example"),
        ("remote", "0.0.0.0", "https://mcp.remote.example"),
    ],
)
def test_endpoint_coordinates_propagate_for_supported_deployments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    name: str,
    host: str,
    public_url: str,
) -> None:
    monkeypatch.setenv("DOMOAI_MCP_HOST", host)
    monkeypatch.setenv("DOMOAI_MCP_PORT", "8124")
    monkeypatch.setenv("DOMOAI_MCP_PUBLIC_URL", public_url)
    if host != "127.0.0.1":
        monkeypatch.setenv("DOMOAI_MCP_CLIENT_TOKEN_FILE", str(tmp_path / f"{name}.json"))
    else:
        monkeypatch.delenv("DOMOAI_MCP_CLIENT_TOKEN_FILE", raising=False)

    settings = Settings.from_environment()

    assert settings.mcp_host == host
    assert settings.mcp_port == 8124
    assert settings.mcp_public_url == public_url


def test_cross_environment_profile_keeps_gateway_and_external_protocol_ports_distinct(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMOAI_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("DOMOAI_MCP_PORT", "8124")
    monkeypatch.setenv("DOMOAI_MCP_PUBLIC_URL", "https://mcp.home.example")
    monkeypatch.setenv("DOMOAI_MCP_CLIENT_TOKEN_FILE", str(tmp_path / "clients.json"))
    monkeypatch.setenv("DOMOAI_HOME_ASSISTANT_URL", "http://homeassistant:8123")
    monkeypatch.setenv("DOMOAI_HOME_ASSISTANT_TOKEN", "ha-secret")
    monkeypatch.setenv("DOMOAI_KNX_GATEWAY_HOST", "host.docker.internal")
    monkeypatch.setenv("DOMOAI_KNX_GATEWAY_PORT", "3672")
    monkeypatch.setenv("DOMOAI_KNX_GATEWAY_ROUTE_BACK", "0")
    monkeypatch.setenv("DOMOAI_KNX_CONFIG_PATH", "config/knx.json")

    settings = Settings.from_environment()

    assert settings.mcp_port == 8124
    assert settings.knx_gateway_port == 3672
    assert settings.knx_gateway_route_back is False
    assert settings.home_assistant_url == "http://homeassistant:8123"
    assert settings.mcp_port not in {settings.knx_gateway_port, 8123, 1883}
