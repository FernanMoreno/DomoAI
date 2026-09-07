from __future__ import annotations

from pathlib import Path

import pytest

from domoai.config.settings import Settings
from domoai.mcp.gateway import build_gateway
from tests.integration.test_mcp_gateway_http import _write_multi_agent_tokens


@pytest.mark.asyncio
async def test_live_runtime_exposes_authenticated_operational_metrics(tmp_path: Path) -> None:
    token_file = tmp_path / "clients.json"
    tokens = _write_multi_agent_tokens(token_file)
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        mcp_host="127.0.0.1",
        mcp_public_url="http://127.0.0.1:8000",
        mcp_client_token_file=token_file,
        mcp_metrics_enabled=True,
    )
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    try:
        async with gateway.http_client() as client:
            response = await client.get(
                "/metrics",
                headers={"Authorization": f"Bearer {tokens['codex']}"},
            )
    finally:
        await gateway.close()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "domoai_up 1" in response.text
    assert "domoai_command_outcome_total" in response.text
    assert all(secret not in response.text for secret in tokens.values())
