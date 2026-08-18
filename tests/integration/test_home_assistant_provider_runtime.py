from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import SecretStr

from domoai.adapters.home_assistant.provider import HomeAssistantProvider
from domoai.application.runtime_factory import build_runtime
from domoai.application.state_service import StateService
from domoai.config.settings import Settings
from domoai.domain.models import Command, ExecutionStatus
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from tests.fixtures.home_assistant_provider import FakeHomeAssistantProviderClient
from tests.fixtures.simulated_home import simulated_home_entities


def structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


@pytest.mark.asyncio
async def test_provider_runtime_feeds_registry_state_store_and_semantic_mcp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeHomeAssistantProviderClient(simulated_home_entities())

    def fixture_client(_url: str, _token: str) -> FakeHomeAssistantProviderClient:
        return client

    monkeypatch.setattr("domoai.application.runtime_factory.HomeAssistantClient", fixture_client)
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "provider-runtime.sqlite3",
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_provider=True,
        )
    )
    try:
        provider = runtime.provider_registry.get("home_assistant")
        assert isinstance(provider, HomeAssistantProvider)
        assert provider.manifest.provider_id == "home_assistant"
        assert runtime.adapter.adapter_id == "home_assistant"
        assert any(device.type.value == "light" for device in runtime.registry.devices)
        states = await runtime.state_store.all()
        assert any(state.capability == "power" and state.value is False for state in states)

        context = DomoticsMcpContext(
            discovery=runtime.discovery,
            state_service=StateService(runtime.state_store),
            facade=runtime.facade,
            registry=runtime.registry,
            policies=[],
        )
        server = create_domotics_server(context)
        assert [tool.name for tool in await server.list_tools()] == [
            "discover_devices",
            "get_state",
            "get_energy_context",
            "validate_command",
            "validate_plan",
            "request_approval",
            "execute_plan",
            "schedule_plan",
            "cancel_scheduled_plan",
            "reschedule_plan",
            "list_scheduled_plans",
            "schedule_recurring_plan",
            "cancel_recurring_schedule",
            "list_recurring_schedules",
            "list_audit_events",
        ]
        inventory = structured(await server.call_tool("discover_devices", {"refresh": False}))
        light_id = next(
            device["id"] for device in inventory["devices"] if device["type"] == "light"
        )
        state_result = structured(
            await server.call_tool("get_state", {"devices": [light_id], "capabilities": ["power"]})
        )

        assert state_result["states"][0]["value"] is False
        assert state_result["states"][0]["source_ref"]["adapter_id"] == "home_assistant"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_provider_runtime_uses_existing_plan_execution_and_readback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = FakeHomeAssistantProviderClient(simulated_home_entities())

    def fixture_client(_url: str, _token: str) -> FakeHomeAssistantProviderClient:
        return client

    monkeypatch.setattr("domoai.application.runtime_factory.HomeAssistantClient", fixture_client)
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "provider-execution.sqlite3",
            home_assistant_url="http://home-assistant.test",
            home_assistant_token=SecretStr("fixture-token"),
            home_assistant_provider=True,
        )
    )
    try:
        plan = runtime.plan_service.create_plan(
            "provider-plan",
            [
                Command(
                    id="provider-command",
                    device_id="living_room.living-room-main-light",
                    command="turn_on",
                    idempotency_key="provider-intent",
                )
            ],
        )
        result = await runtime.facade.execute_plan(runtime.facade.validate_plan(plan))

        assert result.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
        assert result.outcomes[0].adapter_ref is not None
        assert result.outcomes[0].adapter_ref.external_id == "light.living_room_main"
        assert len(client.service_calls) == 1
    finally:
        await runtime.close()
