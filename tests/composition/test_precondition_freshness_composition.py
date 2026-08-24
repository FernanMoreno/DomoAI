from __future__ import annotations

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, ExecutionStatus, Plan, Precondition, StateStatus
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
@pytest.mark.composition
async def test_state_event_between_preflight_and_jit_blocks_second_write() -> None:
    state_store = StateStore()
    switch_id = "garden.garden-pump"

    class AdapterThatStalesEvidence(SimulatedHomeAdapter):
        async def execute(self, command, execution_context=None):
            if not self.calls:
                snapshot = await state_store.get(switch_id, "power")
                assert snapshot is not None
                await state_store.save(snapshot.model_copy(update={"status": StateStatus.STALE}))
            return await super().execute(command, execution_context)

    adapter = AdapterThatStalesEvidence()
    registry = DeviceRegistry()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    service = PlanService(registry, state_store, PolicyEngine([]), audit)
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    source = await state_store.get(switch_id, "power")
    assert source is not None

    plan = Plan(
        id="composition-precondition-freshness",
        commands=[
            Command(
                id="composition-first-write",
                device_id=light_id,
                command="turn_on",
                idempotency_key="composition-first-write",
            ),
            Command(
                id="composition-second-write",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="composition-second-write",
                preconditions=[
                    Precondition(
                        device_id=switch_id,
                        capability="power",
                        expected=source.value,
                    )
                ],
            ),
        ],
    )

    summary = await PlanExecutor(adapter, service, audit).execute(service.validate(plan))

    assert len(adapter.calls) == 1
    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert summary.outcomes[1].status is ExecutionStatus.REJECTED
