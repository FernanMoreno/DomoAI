from __future__ import annotations

import pytest

from domoai.domain.models import Command
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


def _adapter(adapter_id: str, *, fail_connect: bool = False) -> RecordingAdapter:
    return RecordingAdapter(
        adapter_id, source_snapshot(adapter_id=adapter_id), fail_connect=fail_connect
    )


@pytest.mark.asyncio
async def test_all_healthy_components_are_listed_as_connected() -> None:
    first = _adapter("home_assistant")
    second = _adapter("modbus")
    composite = CompositeAdapter([first, second])
    await composite.connect()

    health = await composite.health()

    assert health.connected is True
    assert health.components is not None
    component_ids = {component.adapter_id: component.connected for component in health.components}
    assert component_ids == {"home_assistant": True, "modbus": True}


@pytest.mark.asyncio
async def test_one_down_adapter_is_individually_identifiable() -> None:
    first = _adapter("home_assistant")
    second = _adapter("modbus", fail_connect=True)
    composite = CompositeAdapter([first, second])
    await composite.connect()

    health = await composite.health()

    assert health.connected is True
    assert health.components is not None
    down = [component for component in health.components if not component.connected]
    assert [component.adapter_id for component in down] == ["modbus"]


@pytest.mark.asyncio
async def test_transient_child_unavailability_remains_eligible_for_discovery() -> None:
    child = _adapter("knx")
    composite = CompositeAdapter([child])
    await composite.connect()
    child.available = False

    health = await composite.health()

    assert health.connected is False
    assert health.components is not None
    assert health.components[0].connected is False

    # A physical/availability failure is not the same as losing the
    # transport lifecycle slot.  The supervisor must be able to reconnect or
    # rediscover this child on the next cycle.
    child.available = True
    snapshot = await composite.discover()
    assert snapshot.source_entities


@pytest.mark.asyncio
async def test_two_down_adapters_are_both_individually_identifiable() -> None:
    first = _adapter("home_assistant", fail_connect=True)
    second = _adapter("modbus", fail_connect=True)
    third = _adapter("knx")
    composite = CompositeAdapter([first, second, third])
    await composite.connect()

    health = await composite.health()

    assert health.components is not None
    down_ids = {component.adapter_id for component in health.components if not component.connected}
    assert down_ids == {"home_assistant", "modbus"}


@pytest.mark.asyncio
async def test_composite_forwards_the_same_execution_context_to_child() -> None:
    child = _adapter("fixture")
    registry = DeviceRegistry()
    composite = CompositeAdapter([child], registry=registry)
    await composite.connect()
    snapshot = await composite.discover()
    registry.apply_snapshot(snapshot, "fixture")

    context = ExecutionContext(
        agent_request_id="agent-composite-1",
        plan_id="plan-composite-1",
        execution_attempt_id="attempt-composite-1",
        adapter_request_id="adapter-composite-1",
    )
    await composite.execute(
        Command(
            id="composite-command-1",
            device_id="living_room.main_light",
            command="turn_on",
            idempotency_key="composite-intent-1",
        ),
        context,
    )

    assert child.execution_contexts == [context]
