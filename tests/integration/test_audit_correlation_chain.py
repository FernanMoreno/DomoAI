import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, Plan
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import ExecutionContext, execution_principal
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


class _ContextRecordingAdapter(SimulatedHomeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.execution_contexts: list[ExecutionContext | None] = []

    async def execute(self, command, execution_context=None):
        self.execution_contexts.append(execution_context)
        return await super().execute(command, execution_context)


@pytest.mark.asyncio
async def test_audit_log_reconstructs_full_lineage_of_one_plan_execution() -> None:
    adapter = _ContextRecordingAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    facade = DomoticsFacade(plan_service, executor)

    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-audit-chain",
        commands=[
            Command(
                id="cmd-audit-chain",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-audit-chain",
            )
        ],
        agent_request_id="agent-req-audit-chain",
    )
    validated = facade.validate_plan(plan)

    with execution_principal("agent-codex"):
        await facade.execute_plan(validated)

    events = [event for event in audit.events if event.payload.get("plan_id") == "plan-audit-chain"]
    started = next(event for event in events if event.event_type == "plan_execution_started")
    outcome_events = [event for event in events if event.event_type == "command_execution_outcome"]
    completed = next(event for event in events if event.event_type == "plan_execution_completed")

    assert started.payload["agent_request_id"] == "agent-req-audit-chain"
    assert started.payload["client_principal_id"] == "agent-codex"
    attempt_id = started.payload["execution_attempt_id"]
    assert attempt_id
    assert outcome_events
    assert all(event.payload["execution_attempt_id"] == attempt_id for event in outcome_events)
    assert all(event.payload["adapter_request_id"] for event in outcome_events)
    assert all(event.payload["client_principal_id"] == "agent-codex" for event in outcome_events)
    assert adapter.execution_contexts
    assert all(
        context is not None and context.client_principal_id == "agent-codex"
        for context in adapter.execution_contexts
    )
    assert completed.payload["execution_attempt_id"] == attempt_id


@pytest.mark.asyncio
async def test_plan_created_without_agent_request_id_gets_none_chain_anchor() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    facade = DomoticsFacade(plan_service, executor)

    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-no-agent-request",
        commands=[
            Command(
                id="cmd-no-agent-request",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-no-agent-request",
            )
        ],
    )
    validated = facade.validate_plan(plan)

    await facade.execute_plan(validated)

    started = next(
        event
        for event in audit.events
        if event.event_type == "plan_execution_started"
        and event.payload.get("plan_id") == "plan-no-agent-request"
    )
    assert started.payload["agent_request_id"] is None
