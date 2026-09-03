from pathlib import Path

import pytest

from domoai.config.settings import Settings
from domoai.mcp.configured import build_configured_server
from domoai.mcp.gateway import build_gateway


@pytest.mark.asyncio
async def test_stdio_and_network_entrypoints_register_the_same_semantic_catalog(
    tmp_path: Path,
) -> None:
    stdio_runtime, stdio_server = await build_configured_server(
        Settings(
            database_path=tmp_path / "stdio.sqlite3",
            mcp_public_url="http://127.0.0.1:8001",
        )
    )
    gateway = await build_gateway(
        Settings(
            database_path=tmp_path / "gateway.sqlite3",
            mcp_public_url="http://127.0.0.1:8000",
        ),
        require_configured_adapter=False,
    )

    try:
        stdio_tools = {tool.name for tool in await stdio_server.list_tools()}
        network_tools = {tool.name for tool in await gateway.server.list_tools()}
        assert stdio_tools == network_tools
    finally:
        await stdio_runtime.close()
        await gateway.close()
