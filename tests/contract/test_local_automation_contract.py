from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.local_automation import LocalAutomationEngine
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.state_service import StateService
from domoai.domain.automation import (
    AutomationRule,
    AutomationTrigger,
    automation_rule_digest,
)
from domoai.domain.models import Command, Plan
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.persistence.repositories import (
    AutomationRuleRepository,
    PlanRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_create_and_list_local_rule_require_standing_approval(tmp_path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "local-rule.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    approval_store = ApprovalStore(operator_token="operator", allow_legacy_token=True)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    local_automation = LocalAutomationEngine(
        AutomationRuleRepository(database),
        plan_service,
        executor,
        audit,
        plan_repository=plan_repository,
        state_store=state_store,
    )
    template = Plan(
        id="local-rule-template",
        commands=[
            Command(
                id="local-rule-command",
                device_id="living_room.living-room-main-light",
                command="turn_on",
                idempotency_key="local-rule-intent",
            )
        ],
    )
    rule = AutomationRule(
        id="local-rule-contract",
        name="Contract rule",
        trigger=AutomationTrigger(
            type="state_changed", device_id="hall.sensor", capability="motion", expected=True
        ),
        plan_template=template,
        scope="home",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    validated = plan_service.validate(template)
    grant = approval_store.issue_legacy(
        validated,
        operator_token="operator",
        recurrence_digest=automation_rule_digest(rule),
    )
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, executor),
        registry=registry,
        policies=[],
        approval_store=approval_store,
        plan_repository=plan_repository,
        local_automation=local_automation,
    )
    server = create_domotics_server(context)

    created = await server.call_tool(
        "create_local_automation_rule",
        {"rule": rule.model_dump(mode="json"), "approval_id": grant.approval_id},
    )
    listed = await server.call_tool("list_local_automation_rules", {})
    created_payload = created[1] if isinstance(created, tuple) else created
    listed_payload = listed[1] if isinstance(listed, tuple) else listed

    assert created_payload["status"] == "enabled"
    assert created_payload["definition_digest"] == rule.definition_digest
    assert listed_payload["rules"][0]["rule_id"] == rule.id
    assert "approval_id" not in listed_payload["rules"][0]

    updated_rule = AutomationRule.model_validate(
        {
            **rule.model_dump(mode="json"),
            "name": "Updated contract rule",
            "definition_digest": None,
        }
    )
    updated_validated = plan_service.validate(updated_rule.plan_template)
    updated_grant = approval_store.issue_legacy(
        updated_validated,
        operator_token="operator",
        recurrence_digest=automation_rule_digest(updated_rule),
    )
    updated = await server.call_tool(
        "update_local_automation_rule",
        {"rule": updated_rule.model_dump(mode="json"), "approval_id": updated_grant.approval_id},
    )
    updated_payload = updated[1] if isinstance(updated, tuple) else updated
    listed_after_update = await server.call_tool("list_local_automation_rules", {})
    listed_after_update_payload = (
        listed_after_update[1] if isinstance(listed_after_update, tuple) else listed_after_update
    )

    assert updated_payload["status"] == "enabled"
    assert updated_payload["definition_digest"] == updated_rule.definition_digest
    assert (
        listed_after_update_payload["rules"][0]["definition_digest"]
        == updated_rule.definition_digest
    )
    assert any(
        event.event_type == "automation_rule_updated"
        for event in audit.events
    )
