"""T011-C8 RED: elicitation really travels over the streamable HTTP gateway (SSE, not JSON mode)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ElicitResult
from tests.integration.test_mcp_gateway_http import (
    _shutdown_gateway_server,
    _wait_for_server,
)

from domoai.config.settings import Settings
from domoai.domain.models import Plan
from domoai.mcp.gateway import build_gateway

pytestmark = pytest.mark.asyncio

TOKEN = "chat-client-secret"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def test_the_gateway_delivers_the_confirmation_to_the_client_and_issues_the_grant(
    tmp_path: Path,
) -> None:
    from tests.security.test_household_consent_security import _c3_device, _c3_plan_dict

    port = _free_port()
    (tmp_path / "clients.json").write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": "chat-client",
                        "token_hash": hashlib.sha256(TOKEN.encode()).hexdigest(),
                        "scopes": ["read", "mutate"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        database_path=tmp_path / "gateway.sqlite3",
        audit_database_path=tmp_path / "audit.sqlite3",
        connection_master_key_path=tmp_path / "master.key",
        mcp_tenant_id="tenant-a",
        mcp_household_id="home-a",
        mcp_deployment_id="edge",
        mcp_host="127.0.0.1",
        mcp_port=port,
        mcp_public_url=f"http://127.0.0.1:{port}",
        mcp_client_token_file=tmp_path / "clients.json",
        mcp_chat_confirmation=True,
    )
    assert settings.mcp_json_response is False
    gateway = await build_gateway(settings, require_configured_adapter=False)
    await gateway.start()
    runtime = gateway.runtime
    runtime.registry.load_persisted([_c3_device("light-a")])
    data = _c3_plan_dict(runtime.clock.now(), "plan-http")
    data["deployment_id"] = "edge"
    await runtime.plan_repository.save(Plan.model_validate(data))
    server = uvicorn.Server(
        uvicorn.Config(gateway.app, host="127.0.0.1", port=port, log_level="error")
    )
    server_task = asyncio.create_task(server.serve())
    messages: list[str] = []

    async def human(_ctx: Any, params: Any) -> ElicitResult:
        messages.append(params.message)
        code = re.search(r"Código de verificación: (\d{4})", params.message).group(1)  # type: ignore[union-attr]
        return ElicitResult(action="accept", content={"aprobar": True, "codigo": code})

    try:
        await _wait_for_server(server)
        endpoint = settings.mcp_public_url + settings.mcp_path
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}) as http:
            async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
                try:
                    async with ClientSession(read, write, elicitation_callback=human) as session:
                        await session.initialize()
                        names = {tool.name for tool in (await session.list_tools()).tools}
                        assert "revoke_approval" in names
                        result = await asyncio.wait_for(
                            session.call_tool(
                                "request_approval",
                                {
                                    "plan_id": "plan-http",
                                    "validation_digest": "sha256:" + "1" * 64,
                                },
                            ),
                            timeout=20,
                        )
                        payload = result.structuredContent or {}
                        assert payload.get("approval_id"), payload
                        assert messages
                finally:
                    await read.aclose()
                    await write.aclose()
    finally:
        await _shutdown_gateway_server(server, server_task, gateway)
