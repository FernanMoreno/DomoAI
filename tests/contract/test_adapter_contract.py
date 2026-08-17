import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter


@pytest.mark.asyncio
async def test_adapter_discovery_is_deterministic_and_source_traceable() -> None:
    adapter = SimulatedHomeAdapter()

    first = await adapter.discover()
    second = await adapter.discover()

    assert first == second
    assert len(first.source_entities) == 6
    assert all(
        entity["entity_id"].startswith(("light.", "switch.", "cover.", "climate.", "sensor."))
        for entity in first.source_entities
    )


@pytest.mark.asyncio
async def test_adapter_marks_unavailable_source_without_dropping_it() -> None:
    adapter = SimulatedHomeAdapter()
    adapter.set_available("cover.bedroom_blind", False)

    snapshot = await adapter.discover()
    cover = next(
        entity
        for entity in snapshot.source_entities
        if entity["entity_id"] == "cover.bedroom_blind"
    )
    position = next(
        state
        for state in snapshot.source_states
        if state["entity_id"] == "cover.bedroom_blind" and state["capability"] == "position"
    )

    assert cover["available"] is False
    assert position["value"] == 50
    assert position["available"] is False
