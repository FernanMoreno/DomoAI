from datetime import UTC, datetime

import pytest

from domoai.application.state_service import StateService
from domoai.domain.models import SourceRef, StateSnapshot, StateStatus
from domoai.runtime.clock import FixedClock
from domoai.runtime.state_store import StateStore


def _snapshot(capability: str, value: object, status: StateStatus) -> StateSnapshot:
    now = datetime(2026, 9, 3, 12, tzinfo=UTC)
    return StateSnapshot(
        device_id="living_room.main_light",
        capability=capability,
        value=value,
        observed_at=now,
        received_at=now,
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id=f"light.{capability}"),
    )


@pytest.mark.asyncio
async def test_fresh_only_reads_exclude_invalid_with_structured_diagnostic() -> None:
    store = StateStore(clock=FixedClock(datetime(2026, 9, 3, 12, tzinfo=UTC)))
    invalid = _snapshot("power", True, StateStatus.CURRENT).model_copy(
        update={"status": StateStatus.INVALID, "value": None}
    )
    await store.save(invalid)

    result = await StateService(store).get_with_diagnostics(
        ["living_room.main_light"],
        allow_stale=False,
    )

    assert result.states == ()
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].device_id == "living_room.main_light"
    assert result.diagnostics[0].capability == "power"
    assert result.diagnostics[0].reason == "invalid"
    assert result.diagnostics[0].status is StateStatus.INVALID


@pytest.mark.asyncio
async def test_default_get_keeps_legacy_list_behavior_for_unusable_state() -> None:
    store = StateStore()
    unavailable = _snapshot("power", None, StateStatus.UNAVAILABLE)
    await store.save(unavailable)

    states = await StateService(store).get(["living_room.main_light"])

    assert states == [unavailable]


@pytest.mark.asyncio
async def test_fresh_only_diagnostics_distinguish_stale_and_unavailable() -> None:
    store = StateStore()
    await store.save(_snapshot("power", 1, StateStatus.STALE))
    await store.save(_snapshot("brightness", None, StateStatus.UNAVAILABLE))

    result = await StateService(store).get_with_diagnostics(
        ["living_room.main_light"],
        allow_stale=False,
    )

    assert result.states == ()
    assert {(item.capability, item.reason) for item in result.diagnostics} == {
        ("power", "stale"),
        ("brightness", "unavailable"),
    }
