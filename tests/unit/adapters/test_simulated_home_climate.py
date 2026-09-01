import time

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.domain.models import Command, SourceRef


def _set_temperature_command(value: float) -> Command:
    return Command(
        id="climate-set-1",
        device_id="bedroom.bedroom-climate",
        command="set_temperature",
        value=value,
        idempotency_key="climate-set-temperature-1",
    )


def _read_temperature(states: list, capability: str) -> float:
    return next(state.value for state in states if state.capability == capability)


@pytest.mark.asyncio
async def test_climate_state_is_unchanged_before_any_command() -> None:
    # Spec 165 FR-007-adjacent guarantee for this specific fixture: doing
    # nothing must not change anything, matching every other fixture entity.
    adapter = SimulatedHomeAdapter()
    ref = [SourceRef(adapter_id="fixture", external_id="climate.bedroom")]

    first = await adapter.read_state(ref)
    second = await adapter.read_state(ref)

    assert _read_temperature(first, "temperature") == 20
    assert _read_temperature(second, "temperature") == 20


@pytest.mark.asyncio
async def test_set_temperature_command_is_accepted_and_moves_temperature_over_time() -> None:
    adapter = SimulatedHomeAdapter()
    ref = [SourceRef(adapter_id="fixture", external_id="climate.bedroom")]

    ack = await adapter.execute(_set_temperature_command(25.0))
    assert ack.accepted

    time.sleep(0.05)
    states = await adapter.read_state(ref)

    assert _read_temperature(states, "target_temperature") == 25.0
    temperature = _read_temperature(states, "temperature")
    assert 20.0 < temperature <= 25.0


@pytest.mark.asyncio
async def test_set_temperature_below_current_cools_instead_of_heating() -> None:
    adapter = SimulatedHomeAdapter()
    ref = [SourceRef(adapter_id="fixture", external_id="climate.bedroom")]

    ack = await adapter.execute(_set_temperature_command(16.0))
    assert ack.accepted

    time.sleep(0.05)
    states = await adapter.read_state(ref)

    assert _read_temperature(states, "temperature") < 20.0
