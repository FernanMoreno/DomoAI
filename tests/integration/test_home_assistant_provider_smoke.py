"""Opt-in live smoke for the Home Assistant Provider runtime bridge."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.adapters.home_assistant.provider_adapter import HomeAssistantProviderAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server


def _structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


@pytest.mark.asyncio
async def test_home_assistant_provider_runtime_live_smoke(tmp_path: Path) -> None:
    base_url = os.getenv("DOMOAI_HOME_ASSISTANT_URL")
    token = os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN")
    if not base_url or not token:
        pytest.skip(
            "Set DOMOAI_HOME_ASSISTANT_URL and DOMOAI_HOME_ASSISTANT_TOKEN "
            "for the Home Assistant Provider runtime smoke"
        )

    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "home-assistant-provider-live.sqlite3",
            home_assistant_url=base_url,
            home_assistant_token=SecretStr(token),
            home_assistant_provider=True,
        )
    )
    try:
        provider = runtime.provider_registry.get("home_assistant")
        assert isinstance(provider, HomeAssistantProvider)
        assert isinstance(runtime.adapter, HomeAssistantProviderAdapter)
        assert await runtime.adapter.health()

        source_snapshot = await provider.snapshot()
        assert source_snapshot.source_entities
        assert runtime.registry.devices

        states = await runtime.state_store.all()
        assert states, "Home Assistant must expose at least one readable semantic state"
        state = states[0]

        context = DomoticsMcpContext(
            discovery=runtime.discovery,
            state_service=StateService(runtime.state_store),
            facade=runtime.facade,
            registry=runtime.registry,
            policies=[],
        )
        server = create_domotics_server(context)
        inventory = _structured(
            await server.call_tool("discover_devices", {"refresh": False})
        )
        assert inventory["devices"]
        assert any(device["id"] == state.device_id for device in inventory["devices"])

        state_result = _structured(
            await server.call_tool(
                "get_state",
                {
                    "devices": [state.device_id],
                    "capabilities": [state.capability],
                    "allow_stale": False,
                },
            )
        )
        assert state_result["states"]
        assert state_result["states"][0]["source_ref"]["adapter_id"] == "home_assistant"
    finally:
        await runtime.close()
