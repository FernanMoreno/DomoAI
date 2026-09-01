from pathlib import Path
from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.commissioning import CommissioningService
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
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


@pytest.mark.asyncio
async def test_mcp_exposes_read_only_commissioning_report(tmp_path: Path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    commissioning = CommissioningService(
        registry,
        manifest_path=tmp_path / "commissioning.json",
    )
    report = commissioning.inspect(runtime_revision=state_store.runtime_revision)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
        commissioning_service=commissioning,
        commissioning_report=report,
    )
    server = create_domotics_server(context)

    tool_names = {tool.name for tool in await server.list_tools()}
    result = structured(await server.call_tool("inspect_commissioning", {"refresh": False}))

    assert "inspect_commissioning" in tool_names
    assert result["schema_version"] == "v1"
    assert result["report_digest"] == report.report_digest
    assert result["authority_created"] is False


@pytest.mark.asyncio
async def test_mcp_refresh_is_explicit_and_uses_latest_runtime_revision(tmp_path: Path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    commissioning = CommissioningService(registry, manifest_path=tmp_path / "report.json")
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
        commissioning_service=commissioning,
    )
    server = create_domotics_server(context)

    result = structured(await server.call_tool("inspect_commissioning", {"refresh": True}))

    assert result["runtime_revision"] == "rev-1"
    assert result["authority_created"] is False
