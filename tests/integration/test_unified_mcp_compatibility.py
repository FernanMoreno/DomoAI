import json
from typing import cast

import anyio
import pytest
from mcp import ClientSession
from pydantic import AnyUrl

from domoai.mcp.unified_server import UnifiedMcpContext, create_unified_server
from tests.contract.test_unified_mcp_contract import build_context, scenario_for, structured


def normalized(payload: object) -> str:
    value = json.loads(json.dumps(payload, default=str))
    if isinstance(value, dict):
        plan = value.get("plan")
        if isinstance(plan, dict):
            plan.pop("created_at", None)
            validation = plan.get("validation")
            if isinstance(validation, dict):
                validation.pop("validated_at", None)
    return json.dumps(value, sort_keys=True)


async def client_flow(
    context: UnifiedMcpContext, device_id: str
) -> tuple[list[str], list[str], str]:
    server = create_unified_server(context)
    client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
    server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)

    async def run_server() -> None:
        await server._mcp_server.run(
            client_to_server_receive,
            server_to_client_send,
            server._mcp_server.create_initialization_options(),
        )

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(run_server)
        async with ClientSession(server_to_client_receive, client_to_server_send) as session:
            await session.initialize()
            tools = await session.list_tools()
            resources = await session.list_resources()
            await session.read_resource(cast(AnyUrl, "domotics://devices"))
            proposal = await session.call_tool(
                "optimize_scenario",
                {"scenario": scenario_for(device_id).model_dump(mode="json")},
            )
        task_group.cancel_scope.cancel()

    return (
        [tool.name for tool in tools.tools],
        [str(resource.uri) for resource in resources.resources],
        normalized(proposal.structuredContent),
    )


@pytest.mark.asyncio
async def test_two_independent_clients_receive_equivalent_unified_results() -> None:
    first_adapter, first_context = await build_context()
    second_adapter, second_context = await build_context()
    first_device_id = next(
        device.id
        for device in first_context.domotics.registry.devices
        if device.type.value == "light"
    )
    second_device_id = next(
        device.id
        for device in second_context.domotics.registry.devices
        if device.type.value == "light"
    )

    first = await client_flow(first_context, first_device_id)
    second = await client_flow(second_context, second_device_id)

    assert first == second
    assert first_adapter.calls == []
    assert second_adapter.calls == []


@pytest.mark.asyncio
async def test_protocol_flow_rejects_unknown_scenario_fields_without_adapter_call() -> None:
    adapter, context = await build_context()
    server = create_unified_server(context)

    result = structured(
        await server.call_tool(
            "validate_scenario",
            {"scenario": {"id": "bad", "unexpected": True}},
        )
    )

    assert result["error"]["code"] == "validation_error"
    assert "token" not in str(result).lower()
    assert adapter.calls == []
