import json

import anyio
import pytest
from mcp import ClientSession

from domoai.mcp.ortools_server import OrtoolsMcpContext, create_ortools_server
from tests.integration.test_ortools_mcp_fixtures import build_context, scenario_for


def normalized_json(payload: object) -> str:
    normalized = json.loads(json.dumps(payload, default=str))
    if isinstance(normalized, dict):
        plan = normalized.get("plan")
        if isinstance(plan, dict):
            plan.pop("created_at", None)
            validation = plan.get("validation")
            if isinstance(validation, dict):
                validation.pop("validated_at", None)
        solver_evidence = normalized.get("solver_evidence")
        if isinstance(solver_evidence, dict):
            solver_evidence.pop("wall_time_seconds", None)
        plans = normalized.get("plans")
        if isinstance(plans, list):
            for item in plans:
                if isinstance(item, dict):
                    item.pop("created_at", None)
                    item_validation = item.get("validation")
                    if isinstance(item_validation, dict):
                        item_validation.pop("validated_at", None)
    return json.dumps(normalized, sort_keys=True)


async def call_with_in_process_client(
    context: OrtoolsMcpContext, device_id: str
) -> tuple[list[str], str, str]:
    server = create_ortools_server(context)
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
            scenario = scenario_for(device_id).model_dump(mode="json")
            optimized = await session.call_tool("optimize_scenario", {"scenario": scenario})
            explained = await session.call_tool(
                "explain_solution", {"result": optimized.structuredContent}
            )
        task_group.cancel_scope.cancel()

    return (
        [tool.name for tool in tools.tools],
        normalized_json(optimized.structuredContent),
        normalized_json(explained.structuredContent),
    )


@pytest.mark.asyncio
async def test_two_independent_mcp_clients_receive_equivalent_results() -> None:
    first_adapter, first_context = await build_context()
    second_adapter, second_context = await build_context()
    first_device_id = next(
        device.id for device in first_context.registry.devices if device.type.value == "light"
    )
    second_device_id = next(
        device.id for device in second_context.registry.devices if device.type.value == "light"
    )

    first_tools, first_result, first_explanation = await call_with_in_process_client(
        first_context, first_device_id
    )
    second_tools, second_result, second_explanation = await call_with_in_process_client(
        second_context, second_device_id
    )

    assert (
        first_tools
        == second_tools
        == [
            "validate_scenario",
            "optimize_scenario",
            "explain_solution",
        ]
    )
    assert first_result == second_result
    assert first_explanation == second_explanation
    assert first_adapter.calls == []
    assert second_adapter.calls == []
