from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.config.settings import Settings


def test_gateway_defaults_are_loopback_and_use_the_mcp_path() -> None:
    settings = Settings()

    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 8000
    assert settings.mcp_path == "/mcp"
    assert settings.mcp_public_url == "http://127.0.0.1:8000"
    assert settings.mcp_json_response is True
    assert settings.mcp_server_sent_events is False


def test_gateway_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMOAI_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("DOMOAI_MCP_PORT", "9100")
    monkeypatch.setenv("DOMOAI_MCP_PATH", "/semantic-mcp")
    monkeypatch.setenv("DOMOAI_MCP_PUBLIC_URL", "https://home.example/mcp")
    monkeypatch.setenv("DOMOAI_MCP_CLIENT_TOKEN_FILE", "secrets/mcp-tokens.json")
    monkeypatch.setenv("DOMOAI_MCP_DEPLOYMENT_ID", "home-main")
    monkeypatch.setenv("DOMOAI_MCP_JSON_RESPONSE", "false")
    monkeypatch.setenv("DOMOAI_MCP_SERVER_SENT_EVENTS", "true")

    settings = Settings.from_environment()

    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 9100
    assert settings.mcp_path == "/semantic-mcp"
    assert settings.mcp_public_url == "https://home.example/mcp"
    assert settings.mcp_client_token_file == Path("secrets/mcp-tokens.json")
    assert settings.mcp_deployment_id == "home-main"
    assert settings.mcp_json_response is False
    assert settings.mcp_server_sent_events is True


def test_non_local_gateway_requires_bearer_tokens_and_https_public_url() -> None:
    with pytest.raises(ValidationError, match="client token"):
        Settings(mcp_host="0.0.0.0")

    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(
            mcp_host="0.0.0.0",
            mcp_client_token_file=Path("tokens.json"),
            mcp_public_url="http://192.0.2.10:8000",
        )


def test_gateway_path_must_be_absolute_url_path() -> None:
    with pytest.raises(ValidationError, match="MCP path"):
        Settings(mcp_path="mcp")


def test_audit_database_must_be_physically_separate_from_authority_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "domoai.sqlite3"

    with pytest.raises(ValidationError, match="audit database"):
        Settings(database_path=database_path, audit_database_path=database_path)
