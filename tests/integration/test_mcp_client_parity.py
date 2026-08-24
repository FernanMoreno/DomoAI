from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


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
    )


@pytest.mark.asyncio
async def test_two_mcp_clients_have_equivalent_read_and_safe_execution_results() -> None:
    context = await build_context()
    adapter = cast(SimulatedHomeAdapter, context.facade.executor.adapter)
    client_a = create_domotics_server(context)
    client_b = create_domotics_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    command = {
        "id": "mcp-command-1",
        "device_id": device_id,
        "command": "set_brightness",
        "value": 60,
        "unit": "%",
        "idempotency_key": "mcp-intent-1",
    }

    discovery_a = structured(await client_a.call_tool("discover_devices", {"refresh": False}))
    discovery_b = structured(await client_b.call_tool("discover_devices", {"refresh": False}))
    validation_a = structured(await client_a.call_tool("validate_command", {"command": command}))
    validation_b = structured(await client_b.call_tool("validate_command", {"command": command}))

    assert discovery_a == discovery_b
    assert validation_a["command"] == validation_b["command"]
    for key in ("status", "runtime_revision", "errors", "digest"):
        assert validation_a["validation"][key] == validation_b["validation"][key]
    assert validation_a["validation"]["status"] == "valid"

    plan = structured(
        await client_a.call_tool(
            "validate_plan",
            {"plan": {"id": "mcp-plan-1", "commands": [command]}},
        )
    )
    execution = structured(
        await client_b.call_tool(
            "execute_plan",
            {
                "plan_id": "mcp-plan-1",
                "validation_digest": plan["validation"]["digest"],
                "dry_run": False,
            },
        )
    )

    assert execution["outcomes"][0]["status"] == "confirmed_success"
    assert [call.id for call in adapter.calls] == ["mcp-command-1"]
