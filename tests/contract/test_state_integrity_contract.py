from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.domain.models import StateStatus
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.mcp.resources import as_json
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def structured(result: object) -> dict[str, Any]:
    protocol_content = getattr(result, "structuredContent", None)
    if isinstance(protocol_content, dict):
        return protocol_content

    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return result[1]
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


@pytest.mark.asyncio
async def test_get_state_explains_fresh_only_exclusions_additively() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
    )
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    state = await state_store.get(device_id, "power")
    assert state is not None
    await state_store.save(
        state.model_copy(update={"status": StateStatus.INVALID, "value": None})
    )

    result = structured(
        await create_domotics_server(context).call_tool(
            "get_state",
            {"devices": [device_id], "capabilities": ["power"], "allow_stale": False},
        )
    )

    assert result["schema_version"] == "v1"
    assert result["states"] == []
    assert result["diagnostics"] == [
        {
            "device_id": device_id,
            "capability": "power",
            "reason": "invalid",
            "status": "invalid",
        }
    ]


def test_state_response_serializer_rejects_non_finite_json_numbers() -> None:
    with pytest.raises(ValueError, match="Out of range float values"):
        as_json({"value": float("nan")})
