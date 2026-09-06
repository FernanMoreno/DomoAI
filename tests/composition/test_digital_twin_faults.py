from __future__ import annotations

from datetime import timedelta

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.domain.errors import DomainError
from domoai.domain.models import (
    Command,
    ExecutionStatus,
    Plan,
    Precondition,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from tests.composition.test_digital_twin_composition import _runtime


def _plan(plan_id: str, *, command: str = "turn_on", with_precondition: bool = False) -> Plan:
    return Plan(
        id=plan_id,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="fixture.switch",
                command=command,
                idempotency_key=f"{plan_id}:key",
                preconditions=(
                    [
                        Precondition(
                            device_id="fixture.switch",
                            capability="power",
                            expected=False,
                        )
                    ]
                    if with_precondition
                    else []
                ),
            )
        ],
    )


@pytest.mark.asyncio
async def test_stale_source_after_validation_fails_closed_without_a_write() -> None:
    plant, adapter, registry, state_store, audit, plan_service, executor = await _runtime()
    plan = plan_service.validate(_plan("twin-stale", with_precondition=True))
    plant.set_fault("fixture", "fixture.switch", "stale")
    refreshed = await DiscoveryService(adapter, registry, state_store, audit).refresh_state()

    assert any(snapshot.status is StateStatus.STALE for snapshot in refreshed)
    with pytest.raises(DomainError):
        await executor.execute(plan)

    assert plant.write_count == 0
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_partial_failure_and_replay_are_safe_and_idempotent() -> None:
    plant, adapter, _registry, _state_store, _audit, plan_service, executor = await _runtime()
    partial = plan_service.validate(_plan("twin-partial"))
    plant.set_fault("fixture", "fixture.switch", "partial_failure")

    failed = await executor.execute(partial)

    assert failed.outcomes[0].status is ExecutionStatus.REJECTED
    assert plant.write_count == 0

    plant.set_fault("fixture", "fixture.switch", None)
    replay_plan = plan_service.validate(_plan("twin-replay"))
    first = await executor.execute(replay_plan)
    second = await executor.execute(replay_plan)

    assert first.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert second.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert plant.write_count == 1
    assert any(entry["operation"] == "command_duplicate" for entry in plant.trace)
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_out_of_order_and_duplicate_events_do_not_regress_durable_state() -> None:
    plant, adapter, _registry, state_store, _audit, _plan_service, _executor = await _runtime()
    observed_at = plant.clock.now()

    assert plant.deliver_event(
        "fixture",
        "fixture.switch",
        "power",
        True,
        observed_at=observed_at,
        revision=10,
    )
    assert not plant.deliver_event(
        "fixture",
        "fixture.switch",
        "power",
        False,
        observed_at=observed_at + timedelta(seconds=1),
        revision=9,
    )
    assert not plant.deliver_event(
        "fixture",
        "fixture.switch",
        "power",
        True,
        observed_at=observed_at,
        revision=10,
    )
    observation = plant.read("fixture", "fixture.switch", "power")
    await state_store.save(
        StateSnapshot(
            device_id="fixture.switch",
            capability="power",
            value=observation.value,
            observed_at=observation.observed_at,
            received_at=plant.clock.now(),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="fixture.switch"),
        )
    )

    assert state_store.peek("fixture.switch", "power").value is True  # type: ignore[union-attr]
    assert [entry["operation"] for entry in plant.trace if entry["operation"] == "event_discarded"]
    await adapter.disconnect()
