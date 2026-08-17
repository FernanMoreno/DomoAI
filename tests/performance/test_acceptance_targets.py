from copy import deepcopy
from time import perf_counter

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter, default_entities
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


def entities_for_count(count: int) -> list[dict[str, object]]:
    template = default_entities()[0]
    entities: list[dict[str, object]] = []
    for index in range(count):
        entity = deepcopy(template)
        entity["entity_id"] = f"light.performance_{index}"
        entity["device_id"] = f"performance-device-{index}"
        entity["identity_keys"] = [f"fixture:device:performance-device-{index}"]
        entity["connections"] = [f"fixture:performance-device-{index}"]
        entity["name"] = f"Performance light {index}"
        entity["area_id"] = f"area_{index % 10}"
        entities.append(entity)
    return entities


@pytest.mark.asyncio
async def test_discovery_200_entities_meets_acceptance_target() -> None:
    adapter = SimulatedHomeAdapter(entities_for_count(200))
    service = DiscoveryService(adapter, DeviceRegistry(), StateStore(), AuditLog())

    started = perf_counter()
    result = await service.refresh()
    elapsed = perf_counter() - started

    assert len(result.devices) == 200
    assert elapsed < 10


@pytest.mark.asyncio
async def test_50_command_plan_validation_meets_acceptance_target() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    service = PlanService(registry, state_store, PolicyEngine([]), audit)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="performance-plan-50",
        commands=[
            Command(
                id=f"performance-command-{index}",
                device_id=device_id,
                command="set_brightness",
                value=index % 101,
                unit="%",
                idempotency_key=f"performance-intent-{index}",
            )
            for index in range(50)
        ],
    )

    started = perf_counter()
    validated = service.validate(plan)
    elapsed = perf_counter() - started

    assert validated.validation is not None
    assert validated.validation.status.value == "valid"
    assert elapsed < 1


@pytest.mark.asyncio
async def test_50_command_plan_execution_returns_terminal_outcomes() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, service, audit)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="performance-execution-50",
        commands=[
            Command(
                id=f"execution-command-{index}",
                device_id=device_id,
                command="set_brightness",
                value=index % 101,
                unit="%",
                idempotency_key=f"execution-intent-{index}",
            )
            for index in range(50)
        ],
    )

    summary = await executor.execute(service.validate(plan))

    assert len(summary.outcomes) == 50
    assert all(outcome.status.value == "confirmed_success" for outcome in summary.outcomes)
