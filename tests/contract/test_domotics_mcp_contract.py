import json
from typing import Any, cast

import anyio
import pytest
from mcp import ClientSession

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.state_service import StateService
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.optimizer.energy import StaticEnergyContextProvider
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


def structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


async def build_context() -> DomoticsMcpContext:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
        energy_context_provider=StaticEnergyContextProvider(energy_context_for()),
    )


async def build_composed_context() -> DomoticsMcpContext:
    home_assistant = RecordingAdapter(
        "home_assistant", source_snapshot(adapter_id="home_assistant")
    )
    native = RecordingAdapter(
        "modbus", source_snapshot(adapter_id="modbus", include_shared_device=False)
    )
    registry = DeviceRegistry()
    adapter = CompositeAdapter([home_assistant, native], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await adapter.connect()
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    facade = DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit))
    return DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=facade,
        registry=registry,
        policies=[],
    )


@pytest.mark.asyncio
async def test_mcp_v1_exposes_stable_semantic_surface() -> None:
    server = create_domotics_server(await build_context())

    tools = [tool.name for tool in await server.list_tools()]
    resources = [str(resource.uri) for resource in await server.list_resources()]

    assert tools == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "validate_command",
        "validate_plan",
        "execute_plan",
    ]
    assert resources == [
        "domotics://areas",
        "domotics://capabilities",
        "domotics://devices",
        "domotics://energy",
        "domotics://policies",
    ]


@pytest.mark.asyncio
async def test_invalid_mcp_command_returns_safe_error_envelope_without_adapter_call() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    result = structured(await server.call_tool(
        "validate_command",
        {
            "command": {
                "device_id": device_id,
                "command": "set_brightness",
                "value": 140,
                "idempotency_key": "invalid-intent",
            }
        },
    ))

    assert result["error"]["code"] == "validation_error"
    assert adapter.calls == []
    assert "token" not in str(result).lower()


@pytest.mark.asyncio
async def test_resource_snapshots_are_json_and_versioned() -> None:
    server = create_domotics_server(await build_context())

    contents = list(await server.read_resource("domotics://devices"))

    assert len(contents) == 1
    assert '"schema_version":"v1"' in contents[0].content


@pytest.mark.asyncio
async def test_composed_runtime_keeps_mcp_surface_semantic_and_aggregated() -> None:
    context = await build_composed_context()
    server = create_domotics_server(context)

    tools = [tool.name for tool in await server.list_tools()]
    inventory = structured(await server.call_tool("discover_devices", {"refresh": False}))
    validation = structured(
        await server.call_tool(
            "validate_command",
            {
                "command": {
                    "id": "composed-mcp-command",
                    "device_id": "living_room.main_light",
                    "command": "turn_on",
                    "idempotency_key": "composed-mcp-key",
                }
            },
        )
    )

    assert tools == [
        "discover_devices",
        "get_state",
        "get_energy_context",
        "validate_command",
        "validate_plan",
        "execute_plan",
    ]
    assert {device["id"] for device in inventory["devices"]} >= {
        "living_room.main_light",
        "home_assistant.environment",
        "modbus.environment",
    }
    assert validation["validation"]["status"] == "valid"
    assert "register" not in str(inventory).lower()


@pytest.mark.asyncio
async def test_mcp_energy_context_is_typed_read_only_and_horizon_bounded() -> None:
    context = await build_context()
    server = create_domotics_server(context)
    requested = energy_context_for().horizon

    result = structured(
        await server.call_tool(
            "get_energy_context",
            {"horizon": requested.model_dump(mode="json")},
        )
    )

    assert result["schema_version"] == "v1"
    assert result["context"]["horizon"]["resolution_minutes"] == 15
    assert "protocol" not in str(result["context"]).lower()


async def call_with_in_process_client(context: DomoticsMcpContext) -> tuple[list[str], str]:
    server = create_domotics_server(context)
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
            result = await session.call_tool("discover_devices", {"refresh": False})
        task_group.cancel_scope.cancel()

    return [tool.name for tool in tools.tools], json.dumps(
        result.structuredContent, sort_keys=True, default=str
    )


@pytest.mark.asyncio
async def test_two_mcp_clients_receive_equivalent_contract_results() -> None:
    first_tools, first_result = await call_with_in_process_client(await build_context())
    second_tools, second_result = await call_with_in_process_client(await build_context())

    assert first_tools == second_tools
    assert first_result == second_result
