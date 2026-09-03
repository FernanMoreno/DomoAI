from pathlib import Path

import pytest

from domoai.application.runtime_factory import create_adapter
from domoai.config.settings import Settings
from domoai.mcp.configured import build_configured_server
from domoai.mcp.gateway import build_gateway


def test_strict_runtime_bootstrap_rejects_missing_provider_instead_of_simulating() -> None:
    try:
        create_adapter(Settings(), require_configured_adapter=True)
    except ValueError as error:
        assert "configured adapter" in str(error)
    else:
        raise AssertionError("strict runtime bootstrap unexpectedly selected a simulator")


@pytest.mark.asyncio
async def test_configured_server_rejects_missing_token_file_before_runtime_resources(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "must-not-open.sqlite3",
        mcp_host="0.0.0.0",
        mcp_public_url="https://gateway.example.test",
        mcp_client_token_file=tmp_path / "missing-clients.json",
    )

    with pytest.raises(ValueError, match="MCP client token file"):
        await build_configured_server(settings)

    assert not settings.database_path.exists()


@pytest.mark.asyncio
async def test_gateway_factory_is_strict_by_default(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "must-not-open.sqlite3")

    with pytest.raises(ValueError, match="configured adapter"):
        await build_gateway(settings)

    assert not settings.database_path.exists()
