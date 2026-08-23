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


class _BlockingStructuralAdapter:
    def __init__(self, adapter_id: str, count: int) -> None:
        self.adapter_id = adapter_id
        self.count = count
        self.yielded = 0
        self.cancelled = False

    async def connect(self) -> None:
        return None

    async def subscribe_events(self) -> AsyncIterator[SourceEvent]:
        try:
            for i in range(self.count):
                self.yielded += 1
                yield AvailabilityChangedEvent(payload={"index": i})
        finally:
            self.cancelled = True


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
    assert composite.dropped_events_total == 15
    assert composite.dropped_events_by_adapter == {"home_assistant": 15}
    assert composite.dropped_events_by_kind == {"state_changed": 15}


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

    assert composite.dropped_events_by_adapter == {"home_assistant": 15, "modbus": 20}


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
    assert composite.dropped_events_by_adapter == {"home_assistant": 15}


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
async def test_priority_lane_is_bounded_and_delivers_structural_flood() -> None:
    adapter = _idle_adapter("modbus")
    adapter.events = [
        AvailabilityChangedEvent(payload={"index": i}) for i in range(20)
    ]
    composite = CompositeAdapter([adapter], event_queue_max_size=3)
    await composite.connect()

    delivered = await _drain(composite)

    assert composite._priority_queue is not None
    assert composite._priority_queue.maxsize == 3
    assert len([event for event in delivered if event.kind == "availability_changed"]) == 20


@pytest.mark.asyncio
async def test_cancellation_releases_a_structural_producer_blocked_by_backpressure() -> None:
    adapter = _BlockingStructuralAdapter("modbus", count=100)
    composite = CompositeAdapter([adapter], event_queue_max_size=1)
    await composite.connect()
    stream = composite.subscribe_events()

    await anext(stream)
    await asyncio.sleep(0)

    assert composite.event_queue_depth["priority"] <= 1
    assert adapter.yielded < 100

    await stream.aclose()
    await asyncio.sleep(0)

    assert adapter.cancelled is True


@pytest.mark.asyncio
async def test_identical_state_updates_coalesce_to_the_latest_pending_value() -> None:
    adapter = _idle_adapter("home_assistant")
    adapter.events = [
        StateChangedEvent(
            payload={
                "entity_id": "light.main",
                "capabilities": ["brightness"],
                "value": i,
            }
        )
        for i in range(20)
    ]
    composite = CompositeAdapter([adapter], event_queue_max_size=1)
    await composite.connect()

    delivered = await _drain(composite)

    assert len(delivered) == 1
    assert delivered[0].payload["value"] == 19
    assert composite.coalesced_events_total == 19
    assert composite.dropped_events_total == 0


@pytest.mark.asyncio
async def test_state_coalescing_keeps_same_identity_namespaced_by_adapter() -> None:
    first = _idle_adapter("home_assistant")
    first.events = [
        StateChangedEvent(
            payload={"entity_id": "light.shared", "capabilities": ["power"], "value": i}
        )
        for i in range(10)
    ]
    second = _idle_adapter("modbus")
    second.events = [
        StateChangedEvent(
            payload={"entity_id": "light.shared", "capabilities": ["power"], "value": i}
        )
        for i in range(10, 20)
    ]
    composite = CompositeAdapter([first, second], event_queue_max_size=2)
    await composite.connect()

    delivered = await _drain(composite)

    assert {
        event.payload["source_adapter_id"] for event in delivered
    } == {"home_assistant", "modbus"}
    assert {event.payload["value"] for event in delivered} == {9, 19}
    assert {event.source_adapter_id for event in delivered} == {"home_assistant", "modbus"}


def test_failure_diagnostics_are_bounded_and_drop_telemetry_is_counter_based() -> None:
    adapter = _idle_adapter("home_assistant")
    composite = CompositeAdapter([adapter], event_queue_max_size=2, diagnostics_max_size=3)

    for index in range(10):
        composite._record_failure("home_assistant", "adapter_failed", RuntimeError(str(index)))
    for index in range(100):
        composite._record_drop(
            "home_assistant", StateChangedEvent(payload={"opaque": index})
        )

    assert len(composite.diagnostics) == 3
    assert composite.dropped_events_total == 100
    assert composite.dropped_events_by_adapter == {"home_assistant": 100}
    assert composite.dropped_events_by_kind == {"state_changed": 100}


@pytest.mark.asyncio
async def test_event_queue_depth_defaults_to_zero_before_streaming() -> None:
    adapter = _idle_adapter("home_assistant")
    composite = CompositeAdapter([adapter], event_queue_max_size=5)
    await composite.connect()

    assert composite.event_queue_depth == {"bulk": 0, "priority": 0}
