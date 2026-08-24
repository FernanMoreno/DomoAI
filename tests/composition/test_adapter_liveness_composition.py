from __future__ import annotations

import asyncio

import pytest

from domoai.domain.models import StateChangedEvent
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.registry import DeviceRegistry
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


class FailsOneStreamAdapter(RecordingAdapter):
    def __init__(self) -> None:
        super().__init__("matter", source_snapshot(adapter_id="matter"))
        self.stream_attempts = 0

    def subscribe_events(self):
        self.stream_attempts += 1
        if self.stream_attempts == 1:
            async def failed_stream():
                raise ConnectionError("matter stream lost")
                yield  # pragma: no cover

            return failed_stream()
        self.events = [StateChangedEvent(payload={"recovered": True})]
        return super().subscribe_events()


class HealthRaisesAdapter(RecordingAdapter):
    async def health(self):
        raise RuntimeError("health endpoint failed")


@pytest.mark.composition
@pytest.mark.asyncio
async def test_dead_child_reconnects_without_hiding_partial_health() -> None:
    failing = FailsOneStreamAdapter()
    healthy = RecordingAdapter("ha", source_snapshot(adapter_id="ha"))
    healthy.events = [StateChangedEvent(payload={"healthy": True})]
    registry = DeviceRegistry()
    composite = CompositeAdapter(
        [failing, healthy],
        registry=registry,
        reconnect_initial_delay=0.05,
        reconnect_max_delay=0.05,
    )
    await composite.connect()
    await composite.discover()

    stream = composite.subscribe_events()
    first = await anext(stream)
    assert first.kind == "adapter_diagnostic"
    health_during_reconnect = await composite.health()
    assert health_during_reconnect.components is not None
    statuses = {
        item.adapter_id: item.connected for item in health_during_reconnect.components
    }
    assert statuses["matter"] is False
    assert statuses["ha"] is True
    await asyncio.sleep(0.06)
    remaining = [event async for event in stream]

    assert any(event.payload.get("recovered") for event in remaining)
    assert failing.stream_attempts == 2
    assert any(
        item.get("event_type") == "adapter_event_stream_failed"
        for item in composite.diagnostics
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_health_exception_remains_an_explicit_disconnected_child() -> None:
    failing = HealthRaisesAdapter("matter", source_snapshot(adapter_id="matter"))
    healthy = RecordingAdapter("ha", source_snapshot(adapter_id="ha"))
    composite = CompositeAdapter([failing, healthy])
    await composite.connect()

    health = await composite.health()

    assert health.components is not None
    by_id = {item.adapter_id: item for item in health.components}
    assert by_id["matter"].connected is False
    assert "health check failed" in (by_id["matter"].message or "")
    assert by_id["ha"].connected is True
