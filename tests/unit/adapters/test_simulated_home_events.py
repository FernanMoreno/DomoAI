"""Event-stream behavior of the deterministic home fixture."""

import asyncio

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter


@pytest.mark.asyncio
async def test_idle_event_stream_stays_open_until_fixture_event() -> None:
    adapter = SimulatedHomeAdapter()
    await adapter.connect()
    stream = adapter.subscribe_events()
    pending = asyncio.create_task(anext(stream))

    await asyncio.sleep(0.05)
    assert not pending.done()

    adapter.rename("light.living_room_main", "Renamed light")
    event = await asyncio.wait_for(pending, timeout=1)
    assert event.kind == "metadata_changed"

    await adapter.disconnect()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=1)
