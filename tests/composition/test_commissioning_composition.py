from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.application.commissioning import CommissioningService
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.domain.models import AdapterSnapshot
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import RecordingAdapter


@pytest.mark.asyncio
async def test_discovery_registry_report_and_mcp_share_one_candidate_view(tmp_path: Path) -> None:
    adapter = RecordingAdapter(
        "fixture",
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "fixture.battery",
                    "source_device_id": "battery-1",
                    "canonical_id": "garage.battery",
                    "identity_keys": ["fixture:battery-1"],
                    "connections": ["fixture:bus:1"],
                    "name": "Battery",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.soc",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery.power",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery.capacity",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery_control",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": True,
                            "commands": ["charge", "discharge", "stop"],
                        },
                    ],
                    "available": True,
                }
            ],
            source_states=[],
        ),
    )
    registry = DeviceRegistry()
    state_store = StateStore(clock=FixedClock(datetime(2026, 8, 31, tzinfo=UTC)))
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit, clock=state_store.clock)
    await discovery.refresh()
    commissioning = CommissioningService(
        registry,
        clock=state_store.clock,
        manifest_path=tmp_path / "report.json",
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

    response = await create_domotics_server(context).call_tool("inspect_commissioning", {})
    payload = response[1] if isinstance(response, tuple) else response

    assert isinstance(payload, dict)
    assert payload["report_digest"] == report.report_digest
    assert payload["candidates"][0]["canonical_device_id"] == "garage.battery"
