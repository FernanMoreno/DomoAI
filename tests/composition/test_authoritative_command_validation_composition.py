from __future__ import annotations

from typing import cast

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.composition
@pytest.mark.asyncio
async def test_mcp_plan_and_jit_paths_cross_adapter_with_one_canonical_unit() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    service = PlanService(registry, state_store, PolicyEngine([]), audit)
    climate_id = next(device.id for device in registry.devices if device.type.value == "climate")
    command = Command(
        id="composition-semantic-command-1",
        device_id=climate_id,
        command="set_temperature",
        value=22,
        idempotency_key="composition-semantic-intent-1",
    )

    validated = service.validate(
        Plan(id="composition-semantic-plan-1", commands=[command])
    )
    assert validated.commands[0].unit == "°C"

    # Simulate a caller/cache boundary handing JIT the pre-normalized command
    # while retaining the validated dependency evidence. JIT must canonicalize
    # again before the adapter boundary.
    tampered = validated.model_copy(
        update={
            "commands": [validated.commands[0].model_copy(update={"unit": None})]
        }
    )
    await PlanExecutor(adapter, service, audit).execute(tampered)

    calls = cast(SimulatedHomeAdapter, adapter).calls
    assert len(calls) == 1
    assert calls[0].unit == "°C"
