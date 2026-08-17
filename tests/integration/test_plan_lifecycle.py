from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, PlanStatus, RiskClass
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def build_plan_context() -> tuple[
    SimulatedHomeAdapter, DeviceRegistry, StateStore, AuditLog, PlanService, PlanExecutor
]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    return adapter, registry, state_store, audit, plan_service, executor


@pytest.mark.asyncio
async def test_valid_plan_previews_then_executes() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-light-1",
        commands=[
            Command(
                id="command-light-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-light-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert validated.status is PlanStatus.READY
    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert outcomes.outcomes[0].after_state is not None
    assert outcomes.outcomes[0].after_state.value == 60
    assert [command.id for command in adapter.calls] == ["command-light-1"]


@pytest.mark.asyncio
async def test_out_of_range_command_is_rejected_before_adapter_call() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-invalid-1",
        commands=[
            Command(
                id="command-invalid-1",
                device_id=device_id,
                command="set_brightness",
                value=140,
                unit="%",
                idempotency_key="intent-invalid-1",
            )
        ],
    )

    validated = plan_service.validate(plan)

    assert validated.status is PlanStatus.VALIDATED
    assert validated.validation is not None
    assert validated.validation.status.value == "invalid"
    with pytest.raises(ValueError):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_sensitive_command_requires_matching_operator_approval() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    plan = Plan(
        id="plan-cover-1",
        commands=[
            Command(
                id="command-cover-1",
                device_id=device_id,
                command="open",
                risk_class=RiskClass.CONFIRM,
                idempotency_key="intent-cover-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    assert validated.status is PlanStatus.REQUIRES_CONFIRMATION
    with pytest.raises(ValueError):
        await executor.execute(validated)
    assert adapter.calls == []

    approved = plan_service.approve(validated, approved_by="local_operator")
    outcomes = await executor.execute(approved)

    assert approved.status is PlanStatus.APPROVED
    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_changed_runtime_revision_requires_revalidation() -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-stale-1",
        commands=[
            Command(
                id="command-stale-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-stale-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    state_store.begin_revision()

    with pytest.raises(ValueError, match="runtime revision"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_cancelled_plan_never_reaches_adapter() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-cancelled-1",
        commands=[
            Command(
                id="command-cancelled-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-cancelled-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    cancelled = plan_service.cancel(validated)

    assert cancelled.status is PlanStatus.CANCELLED
    with pytest.raises(ValueError, match="cancelled"):
        await executor.execute(cancelled)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_expired_plan_is_stale_before_adapter_call() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-expired-1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        commands=[
            Command(
                id="command-expired-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-expired-1",
            )
        ],
    )

    validated = plan_service.validate(plan)

    with pytest.raises(ValueError, match="expired"):
        await executor.execute(validated)
    assert adapter.calls == []
