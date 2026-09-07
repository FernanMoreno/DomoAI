from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.commissioning import CommissioningService
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.domain.commissioning import (
    CommissioningCheck,
    CommissioningEvidence,
    CommissioningEvidenceClass,
)
from domoai.domain.models import AdapterSnapshot
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import RecordingAdapter


@pytest.mark.asyncio
async def test_qualification_is_read_only_and_never_calls_an_adapter() -> None:
    adapter = RecordingAdapter(
        "fixture",
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "fixture.battery",
                    "source_device_id": "battery-1",
                    "canonical_id": "garage.battery",
                    "identity_keys": ["fixture:battery-1"],
                    "connections": ["fixture:battery-1"],
                    "name": "Battery",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": name,
                            "kind": "number",
                            "readable": True,
                            "writable": name == "battery_control",
                            "commands": ["charge"] if name == "battery_control" else [],
                        }
                        for name in (
                            "battery.soc",
                            "battery.power",
                            "battery.capacity",
                            "battery_control",
                        )
                    ],
                    "available": True,
                }
            ]
        ),
    )
    clock = FixedClock(datetime(2026, 9, 4, tzinfo=UTC))
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog(clock=clock)
    discovery = DiscoveryService(adapter, registry, state_store, audit, clock=clock)
    await discovery.refresh()
    commissioning = CommissioningService(registry, clock=clock)
    report = commissioning.inspect(runtime_revision=state_store.runtime_revision, persist=False)
    evidence = CommissioningEvidence(
        authority=report.authority,
        evidence_id="evidence-composition-1",
        candidate_digest=report.candidates[0].candidate_digest,
        observed_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=1),
        evidence_class=CommissioningEvidenceClass.SIMULATION,
        checks=[
            CommissioningCheck(check_id=check, status="passed", detail="fixture")
            for check in ("identity", "read_observation", "safe_actuation", "readback")
        ],
    )
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
        commissioning_service=commissioning,
        commissioning_report=report,
    )

    response = await create_domotics_server(context).call_tool(
        "verify_commissioning", {"evidence": evidence.model_dump(mode="json")}
    )
    payload = response[1] if isinstance(response, tuple) else response

    assert payload["status"] == "blocked_external_dependency"
    assert payload["authority_created"] is False
    assert adapter.writes == []
