from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.facade import DomoticsFacade
from domoai.application.plan_service import PlanService
from domoai.application.state_service import StateService
from domoai.domain.models import Command, Plan, Policy, PolicyAction
from domoai.mcp.domotics_server import DomoticsMcpContext, create_domotics_server
from domoai.persistence.repositories import PlanRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import (
    ApprovalAssertion,
    ApprovalStore,
    OperatorPrincipal,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore


def _structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_approval_schedule_reschedule_boundary_never_reuses_old_consent(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    cover_id = next(device.id for device in registry.devices if device.type.value == "cover")
    database = SQLiteDatabase(tmp_path / "temporal-consent.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    scheduled_repository = ScheduledPlanRepository(database, clock=clock)
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [
                Policy(
                    id="confirm-cover",
                    target={"device_id": cover_id},
                    action=PolicyAction.CONFIRM,
                )
            ]
        ),
        audit,
        clock=clock,
    )
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    scheduler = Scheduler(executor, scheduled_repository, audit, clock=clock)
    context = DomoticsMcpContext(
        discovery=DiscoveryService(adapter, registry, state_store, audit),
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, executor),
        registry=registry,
        policies=[],
        plan_repository=plan_repository,
        scheduler=scheduler,
        approval_store=ApprovalStore(
            operator_token="operator", allow_legacy_token=True, clock=clock
        ),
        clock=clock,
    )
    server = create_domotics_server(context)
    intended = now + timedelta(hours=1)
    validated = _structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "composition-temporal-1",
                    "execute_at": intended.isoformat(),
                    "commands": [
                        {
                            "id": "composition-temporal-command-1",
                            "device_id": cover_id,
                            "command": "open",
                            "idempotency_key": "composition-temporal-intent-1",
                        }
                    ],
                }
            },
        )
    )
    assert validated["validation"]["status"] == "requires_confirmation"
    approval = _structured(
        await server.call_tool(
            "request_approval",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "operator_token": "operator",
            },
        )
    )
    scheduled = _structured(
        await server.call_tool(
            "schedule_plan",
            {
                "plan_id": validated["plan"]["id"],
                "validation_digest": validated["validation"]["digest"],
                "execute_at": intended.isoformat(),
                "approval_id": approval["approval_id"],
            },
        )
    )
    assert "error" not in scheduled

    result = _structured(
        await server.call_tool(
            "reschedule_plan",
            {
                "plan_id": validated["plan"]["id"],
                "execute_at": (intended + timedelta(hours=1)).isoformat(),
            },
        )
    )

    assert result["error"]["code"] == "reschedule_requires_revalidation"
    pending = await scheduled_repository.get(validated["plan"]["id"])
    assert pending is not None
    assert pending[0].execute_at == intended
    assert cast(SimulatedHomeAdapter, context.facade.executor.adapter).calls == []
    assert any(
        event.event_type == "reschedule_rejected" and event.subject_id == validated["plan"]["id"]
        for event in audit.events
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_scheduler_rejects_tampered_window_before_adapter_boundary(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    database = SQLiteDatabase(tmp_path / "tampered-window.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    scheduled_repository = ScheduledPlanRepository(database, clock=clock)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    scheduler = Scheduler(executor, scheduled_repository, audit, clock=clock)
    plan = plan_service.validate(
        Plan(
            id="composition-tampered-window-1",
            execute_at=now + timedelta(hours=1),
            commands=[
                Command(
                    id="composition-tampered-window-command-1",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="composition-tampered-window-intent-1",
                )
            ],
        )
    )
    await plan_repository.save_validation(plan)
    await scheduled_repository.schedule(plan)
    tampered_at = now + timedelta(minutes=1)
    tampered = plan.model_copy(
        update={
            "execute_at": tampered_at,
            "execution_window": plan.execution_window.model_copy(
                update={
                    "intended_at": tampered_at,
                    "not_before": tampered_at,
                    "not_after": tampered_at + timedelta(minutes=1),
                }
            ),
        }
    )
    database.connection.execute(
        "UPDATE scheduled_plans SET execute_at = ?, payload = ? WHERE plan_id = ?",
        (tampered_at.isoformat(), tampered.model_dump_json(), plan.id),
    )
    database.connection.commit()
    clock.set(now + timedelta(minutes=2))

    results = await scheduler.run_due()

    assert results == [{"plan_id": plan.id, "outcome": "failed"}]
    assert (await scheduled_repository.get(plan.id))[1] == "failed"
    assert adapter.calls == []


@pytest.mark.composition
@pytest.mark.asyncio
async def test_authenticated_session_needs_explicit_gesture_before_bundle_approval() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(adapter, registry, state_store, audit)
    await discovery.refresh()
    cover_id = next(device.id for device in registry.devices if device.type.value == "cover")
    principal = OperatorPrincipal("human-42", "oidc:mfa", "session-7")
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [
                Policy(
                    id="confirm-cover-gesture",
                    target={"device_id": cover_id},
                    action=PolicyAction.CONFIRM,
                )
            ]
        ),
        audit,
        clock=clock,
    )
    context = DomoticsMcpContext(
        discovery=discovery,
        state_service=StateService(state_store),
        facade=DomoticsFacade(plan_service, PlanExecutor(adapter, plan_service, audit)),
        registry=registry,
        policies=[],
        approval_store=ApprovalStore(clock=clock),
        operator_principal_provider=lambda: principal,
        clock=clock,
    )
    server = create_domotics_server(context)
    validated = _structured(
        await server.call_tool(
            "validate_plan",
            {
                "plan": {
                    "id": "composition-explicit-approval-1",
                    "commands": [
                        {
                            "id": "composition-explicit-approval-command-1",
                            "device_id": cover_id,
                            "command": "open",
                            "idempotency_key": "composition-explicit-approval-intent-1",
                        }
                    ],
                }
            },
        )
    )
    approval_arguments = {
        "plan_id": validated["plan"]["id"],
        "validation_digest": validated["validation"]["digest"],
        "bundle_digest": "sha256:composition-bundle-1",
    }

    principal_only = _structured(await server.call_tool("request_approval", approval_arguments))
    assert principal_only["error"]["code"] == "approval_assertion_required"

    context.operator_approval_assertion_provider = (
        lambda plan_id, validation_digest, bundle_digest: ApprovalAssertion(
            principal=principal,
            plan_id=plan_id,
            validation_digest=validation_digest,
            bundle_digest=bundle_digest,
            nonce="composition-human-gesture-1",
            approved_at=now,
            expires_at=now + timedelta(minutes=5),
        )
    )
    approved = _structured(await server.call_tool("request_approval", approval_arguments))

    assert approved["plan_id"] == validated["plan"]["id"]
    assert approved["bundle_digest"] == approval_arguments["bundle_digest"]
    grant = context.approval_store.consume(
        approved["approval_id"],
        context.plans[validated["plan"]["id"]],
        bundle_digest=approval_arguments["bundle_digest"],
    )
    assert grant.approved_by == principal.id
    assert grant.assertion_nonce == "composition-human-gesture-1"
