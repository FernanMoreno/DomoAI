"""Two authenticated SDK sessions share one durable runtime profile."""

import hashlib
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from domoai.config.settings import Settings
from domoai.mcp.gateway import build_gateway
from tests.fixtures.household_preferences import mutation, preference


@asynccontextmanager
async def session(gateway, endpoint, token):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app),
        base_url=endpoint.removesuffix("/mcp"),
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        async with streamable_http_client(endpoint, http_client=client) as (read, write, _):
            try:
                async with ClientSession(read, write) as sdk:
                    await sdk.initialize()
                    yield sdk
            finally:
                await read.aclose()
                await write.aclose()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_two_authorized_sessions_retry_restart_and_no_effects(tmp_path):
    token_file = tmp_path / "tokens.json"
    token_file.write_text(
        json.dumps(
            {
                "clients": [
                    {
                        "client_id": name,
                        "token_hash": hashlib.sha256(name.encode()).hexdigest(),
                        "scopes": ["read", "mutate"],
                        "tenant_id": "tenant-a",
                        "household_ids": ["home-a"],
                        "roles": ["owner"],
                    }
                    for name in ("agent-one", "agent-two")
                ]
            }
        )
    )
    settings = Settings(
        database_path=tmp_path / "home.sqlite",
        mcp_client_token_file=token_file,
        mcp_tenant_id="tenant-a",
        mcp_household_id="home-a",
        mcp_public_url="http://127.0.0.1:8124",
    )
    endpoint = settings.mcp_public_url + settings.mcp_path
    confirmed = None
    for run in range(2):
        gateway = await build_gateway(settings, require_configured_adapter=False)
        try:
            async with gateway.app.router.lifespan_context(gateway.app):
                async with (
                    session(gateway, endpoint, "agent-one") as first,
                    session(gateway, endpoint, "agent-two") as second,
                ):
                    result = await first.call_tool("get_household_preferences", {"request": {}})
                    assert not result.isError, "MCP profile read is unavailable"
                    profile = result.structuredContent
                    if run == 0:
                        device = next(
                            d
                            for d in gateway.runtime.registry.devices
                            if any(c.writable for c in d.capabilities)
                        )
                        request = mutation(
                            profile["version"],
                            preference=preference(
                                target={"kind": "device", "id": device.id}, valid_until=None
                            ),
                        )
                        write = await first.call_tool(
                            "set_household_preference", {"request": request}
                        )
                        assert not write.isError
                        confirmed = write.structuredContent
                        retry = await first.call_tool(
                            "set_household_preference", {"request": request}
                        )
                        assert retry.structuredContent == confirmed
                    observed = await second.call_tool("get_household_preferences", {"request": {}})
                    assert not observed.isError
                    assert observed.structuredContent["version"] == confirmed["version"]
                    assert (
                        observed.structuredContent["preferences"][0]["slot"]["preference"]["value"]
                        is False
                    )
                    assert (
                        gateway.runtime.database.connection.execute(
                            "SELECT COUNT(*) FROM plans"
                        ).fetchone()[0]
                        == 0
                    )
                    assert (
                        gateway.runtime.database.connection.execute(
                            "SELECT COUNT(*) FROM execution_outcomes"
                        ).fetchone()[0]
                        == 0
                    )
        finally:
            await gateway.runtime.close()
