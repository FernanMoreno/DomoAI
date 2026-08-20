from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from domoai.domain.models import (
    AvailabilityChangedEvent,
    DeviceMembershipChangedEvent,
    MetadataChangedEvent,
    SourceEvent,
    StateChangedEvent,
)
from domoai.runtime.composite_adapter import CompositeAdapter
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


def _idle_adapter(adapter_id: str) -> RecordingAdapter:
    return RecordingAdapter(adapter_id, source_snapshot(adapter_id=adapter_id))


class _PacedFloodAdapter:
    """Emits events with a real cooperative yield between each, like a live source."""

    def __init__(self, adapter_id: str, count: int) -> None:
        self.adapter_id = adapter_id
        self.count = count

    async def connect(self) -> None:
        return None

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        for i in range(self.count):
            await asyncio.sleep(0)
            yield StateChangedEvent(payload={"i": i})


async def _drain(composite: CompositeAdapter) -> list[SourceEvent]:
    return [event async for event in composite.subscribe_events()]


@pytest.mark.asyncio
async def test_below_threshold_traffic_is_fully_delivered() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [StateChangedEvent(payload={"i": i}) for i in range(5)]
    composite = CompositeAdapter([adapter], event_queue_max_size=1000)
    await composite.connect()

    delivered = await _drain(composite)

    assert len(delivered) == 5
    assert composite.diagnostics == []


@pytest.mark.asyncio
async def test_burst_beyond_threshold_is_capped_and_recorded() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    composite = CompositeAdapter([adapter], event_queue_max_size=5)
    await composite.connect()

    delivered = await _drain(composite)

    assert len(delivered) == 5
    drops = [
        entry
        for entry in composite.diagnostics
        if entry["event_type"] == "adapter_event_dropped_backpressure"
    ]
    assert len(drops) == 15
    assert all(entry["adapter_id"] == "home_assistant" for entry in drops)


@pytest.mark.asyncio
async def test_runtime_continues_normally_after_a_storm_subsides() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    composite = CompositeAdapter([adapter], event_queue_max_size=5)
    await composite.connect()
    await _drain(composite)

    adapter.events = [StateChangedEvent(payload={"tail": True})]
    delivered = await _drain(composite)

    assert len(delivered) == 1
    assert delivered[0].payload["tail"] is True


@pytest.mark.asyncio
async def test_well_behaved_adapter_is_not_starved_by_a_flood() -> None:
    flooding = _PacedFloodAdapter("home_assistant", 50)
    quiet = _idle_adapter("modbus")
    quiet.events = [StateChangedEvent(payload={"quiet": True})]
    composite = CompositeAdapter([flooding, quiet], event_queue_max_size=5)
    await composite.connect()

    delivered = await _drain(composite)

    quiet_events = [
        event for event in delivered if event.payload.get("source_adapter_id") == "modbus"
    ]
    assert len(quiet_events) >= 1


@pytest.mark.asyncio
async def test_drops_from_two_adapters_are_recorded_individually() -> None:
    first = _idle_adapter("home_assistant")
    first.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    second = _idle_adapter("modbus")
    second.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    composite = CompositeAdapter([first, second], event_queue_max_size=5)
    await composite.connect()

    await _drain(composite)

    drop_adapter_ids = {
        entry["adapter_id"]
        for entry in composite.diagnostics
        if entry["event_type"] == "adapter_event_dropped_backpressure"
    }
    assert drop_adapter_ids == {"home_assistant", "modbus"}


@pytest.mark.asyncio
async def test_overflow_does_not_yield_an_extra_diagnostic_source_event() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    composite = CompositeAdapter([adapter], event_queue_max_size=5)
    await composite.connect()

    delivered = await _drain(composite)

    assert len(delivered) == 5
    assert all(event.kind != "adapter_diagnostic" for event in delivered)


@pytest.mark.asyncio
async def test_event_queue_max_size_is_configurable() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [StateChangedEvent(payload={"i": i}) for i in range(10)]
    composite = CompositeAdapter([adapter], event_queue_max_size=3)
    await composite.connect()

    delivered = await _drain(composite)

    assert len(delivered) == 3


@pytest.mark.asyncio
async def test_availability_changed_survives_a_state_changed_flood() -> None:
    flooding = _idle_adapter("home_assistant")
    flooding.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    structural_source = _idle_adapter("modbus")
    structural_source.events = [AvailabilityChangedEvent(payload={"note": "device offline"})]
    composite = CompositeAdapter([flooding, structural_source], event_queue_max_size=5)
    await composite.connect()

    delivered = await _drain(composite)

    structural = [event for event in delivered if event.kind == "availability_changed"]
    assert len(structural) == 1

    drops = [
        entry
        for entry in composite.diagnostics
        if entry["event_type"] == "adapter_event_dropped_backpressure"
    ]
    assert len(drops) == 15
    assert all(entry["adapter_id"] == "home_assistant" for entry in drops)


@pytest.mark.asyncio
async def test_device_membership_changed_survives_a_state_changed_flood() -> None:
    flooding = _idle_adapter("home_assistant")
    flooding.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    structural_source = _idle_adapter("modbus")
    structural_source.events = [DeviceMembershipChangedEvent(payload={"note": "device removed"})]
    composite = CompositeAdapter([flooding, structural_source], event_queue_max_size=5)
    await composite.connect()

    delivered = await _drain(composite)

    structural = [event for event in delivered if event.kind == "device_membership_changed"]
    assert len(structural) == 1


@pytest.mark.asyncio
async def test_metadata_changed_survives_a_state_changed_flood() -> None:
    flooding = _idle_adapter("home_assistant")
    flooding.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    structural_source = _idle_adapter("modbus")
    structural_source.events = [MetadataChangedEvent(payload={"note": "name changed"})]
    composite = CompositeAdapter([flooding, structural_source], event_queue_max_size=5)
    await composite.connect()

    delivered = await _drain(composite)

    structural = [event for event in delivered if event.kind == "metadata_changed"]
    assert len(structural) == 1


@pytest.mark.asyncio
async def test_structural_event_is_not_starved_until_flood_fully_drains() -> None:
    flooding = _PacedFloodAdapter("home_assistant", 50)
    structural_source = _idle_adapter("modbus")
    structural_source.events = [AvailabilityChangedEvent(payload={"note": "device offline"})]
    composite = CompositeAdapter([flooding, structural_source], event_queue_max_size=5)
    await composite.connect()

    delivered_before_end: list[SourceEvent] = []
    async for event in composite.subscribe_events():
        delivered_before_end.append(event)
        if event.kind == "availability_changed":
            break

    assert delivered_before_end
    assert delivered_before_end[-1].kind == "availability_changed"
    assert len(delivered_before_end) < 50


@pytest.mark.asyncio
async def test_dropped_events_total_survives_a_reconnect() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [StateChangedEvent(payload={"i": i}) for i in range(20)]
    composite = CompositeAdapter([adapter], event_queue_max_size=5)
    await composite.connect()

    await _drain(composite)
    assert composite.dropped_events_total == 15

    await composite.connect()

    assert composite.diagnostics == []
    assert composite.dropped_events_total == 15


@pytest.mark.asyncio
async def test_event_queue_depth_defaults_to_zero_before_streaming() -> None:
    adapter = _idle_adapter("home_assistant")
    composite = CompositeAdapter([adapter], event_queue_max_size=5)
    await composite.connect()

    assert composite.event_queue_depth == {"bulk": 0, "priority": 0}
