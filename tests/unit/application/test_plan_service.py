from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.errors import DomainError
from domoai.domain.models import (
    Command,
    Plan,
    Policy,
    PolicyAction,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def build_service(policies: list[Policy] | None = None) -> PlanService:
    adapter, service = await build_service_with_adapter(policies)
    return service


async def build_service_with_adapter(
    policies: list[Policy] | None = None,
) -> tuple[SimulatedHomeAdapter, PlanService]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    return adapter, PlanService(registry, state_store, PolicyEngine(policies or []), AuditLog())


@pytest.mark.asyncio
async def test_create_plan_normalizes_declared_capability_unit() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    command = Command(
        id="command-normalized-1",
        device_id=device_id,
        command="set_brightness",
        value=60,
        idempotency_key="intent-normalized-1",
    )

    plan = service.create_plan("plan-normalized-1", [command])

    assert plan.commands[0].unit == "%"
    assert plan.expires_at is not None
    assert plan.expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_create_plan_rejects_incompatible_unit() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    command = Command(
        id="command-unit-1",
        device_id=device_id,
        command="set_brightness",
        value=60,
        unit="W",
        idempotency_key="intent-unit-1",
    )

    with pytest.raises(DomainError, match="unit"):
        service.create_plan("plan-unit-1", [command])


@pytest.mark.asyncio
async def test_policy_revision_change_invalidates_previous_validation() -> None:
    service = await build_service()
    device_id = next(
        device.id for device in service.registry.devices if device.type.value == "switch"
    )
    plan = service.create_plan(
        "plan-policy-revision-1",
        [
            Command(
                id="command-policy-revision-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-policy-revision-1",
            )
        ],
    )
    validated = service.validate(plan)
    service.policy_engine.policies.append(
        Policy(
            id="deny-policy-revision-1",
            target={"device_id": device_id},
            action=PolicyAction.DENY,
        )
    )

    with pytest.raises(DomainError, match="revision"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_unrelated_state_change_does_not_invalidate_plan() -> None:
    adapter, service = await build_service_with_adapter()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = service.create_plan(
        "plan-unrelated-1",
        [
            Command(
                id="command-unrelated-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-unrelated-1",
            )
        ],
    )
    validated = service.validate(plan)

    unrelated_device_id = next(
        device.id for device in service.registry.devices if device.type.value == "switch"
    )
    await service.state_store.save(
        StateSnapshot(
            device_id=unrelated_device_id,
            capability="power",
            value=True,
            observed_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id=unrelated_device_id),
        )
    )

    executor = PlanExecutor(adapter, service, AuditLog())
    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "confirmed_success"


@pytest.mark.asyncio
async def test_own_state_change_invalidates_plan() -> None:
    adapter, service = await build_service_with_adapter()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = service.create_plan(
        "plan-own-state-1",
        [
            Command(
                id="command-own-state-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-own-state-1",
            )
        ],
    )
    validated = service.validate(plan)

    await service.state_store.save(
        StateSnapshot(
            device_id=light_id,
            capability="power",
            value=True,
            observed_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id=light_id),
        )
    )

    with pytest.raises(DomainError, match="revision"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_validating_unchanged_plan_twice_yields_identical_digest() -> None:
    service = await build_service()
    light_id = next(
        device.id for device in service.registry.devices if device.type.value == "light"
    )
    plan = service.create_plan(
        "plan-digest-1",
        [
            Command(
                id="command-digest-1",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-digest-1",
            )
        ],
    )

    first = service.validate(plan)
    second = service.validate(plan)

    assert first.validation is not None
    assert second.validation is not None
    assert first.validation.digest == second.validation.digest


@pytest.mark.asyncio
async def test_plan_expiry_uses_injected_clock() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    service = PlanService(registry, state_store, PolicyEngine([]), AuditLog(), clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")

    plan = service.create_plan(
        "plan-clock-expiry-1",
        [
            Command(
                id="command-clock-expiry-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-clock-expiry-1",
            )
        ],
    )
    assert plan.expires_at == initial + PlanService.DEFAULT_PLAN_TTL

    validated = service.validate(plan)
    assert validated.validation is not None
    assert validated.validation.validated_at == initial

    clock.set(initial + PlanService.DEFAULT_PLAN_TTL + timedelta(seconds=1))

    with pytest.raises(DomainError, match="expired"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_validate_assigns_default_expiry_when_mcp_plan_omits_it() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    service = PlanService(registry, state_store, PolicyEngine([]), AuditLog(), clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-mcp-default-expiry-1",
        commands=[
            Command(
                id="command-mcp-default-expiry-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-mcp-default-expiry-1",
            )
        ],
    )

    validated = service.validate(plan)

    assert validated.expires_at == initial + PlanService.DEFAULT_PLAN_TTL
    clock.set(initial + PlanService.DEFAULT_PLAN_TTL + timedelta(seconds=1))
    with pytest.raises(DomainError, match="expired"):
        service.assert_executable(validated)


@pytest.mark.asyncio
async def test_validate_keeps_future_plan_valid_through_execution_window() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    await DiscoveryService(adapter, registry, state_store, AuditLog()).refresh()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    service = PlanService(registry, state_store, PolicyEngine([]), AuditLog(), clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    execute_at = initial + timedelta(hours=4)
    plan = Plan(
        id="plan-future-default-expiry-1",
        execute_at=execute_at,
        commands=[
            Command(
                id="command-future-default-expiry-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-future-default-expiry-1",
            )
        ],
    )

    validated = service.validate(plan)

    assert validated.expires_at == execute_at + PlanService.DEFAULT_PLAN_TTL
