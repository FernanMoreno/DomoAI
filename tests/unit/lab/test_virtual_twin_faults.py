from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.lab.virtual_plant import VirtualHomePlant


def test_stale_fault_returns_old_observation_without_marking_source_unavailable() -> None:
    plant = VirtualHomePlant.default(seed=187)
    plant.set_fault("fixture", "fixture.light", "stale")

    observation = plant.read("fixture", "fixture.light", "power")

    assert observation.available is True
    assert observation.observed_at < plant.clock.now()


def test_delayed_fault_is_recorded_but_does_not_sleep_or_change_command_result() -> None:
    plant = VirtualHomePlant.default(seed=187)
    plant.set_fault("fixture", "fixture.light", "delayed")

    observation = plant.command(
        "fixture", "fixture.light", "turn_on", idempotency_key="delayed-1"
    )

    assert observation.value is True
    assert any(entry["operation"] == "command_delayed" for entry in plant.trace)


def test_rejected_and_partial_failure_faults_never_mutate_the_virtual_device() -> None:
    for fault in ("rejected", "partial_failure"):
        plant = VirtualHomePlant.default(seed=187)
        plant.set_fault("fixture", "fixture.light", fault)

        with pytest.raises(ValueError):
            plant.command("fixture", "fixture.light", "turn_on", idempotency_key=fault)

        assert plant.read("fixture", "fixture.light", "power").value is False
        assert plant.write_count == 0


def test_out_of_order_event_is_discarded_by_revision_and_duplicate_is_idempotent() -> None:
    plant = VirtualHomePlant.default(seed=187)
    observed_at = datetime(2026, 9, 4, tzinfo=UTC)

    assert plant.deliver_event(
        "fixture", "fixture.light", "power", True, observed_at=observed_at, revision=1
    )
    assert not plant.deliver_event(
        "fixture",
        "fixture.light",
        "power",
        False,
        observed_at=observed_at - timedelta(seconds=1),
        revision=0,
    )
    assert not plant.deliver_event(
        "fixture", "fixture.light", "power", True, observed_at=observed_at, revision=1
    )

    assert plant.read("fixture", "fixture.light", "power").value is True
    assert any(entry["operation"] == "event_discarded" for entry in plant.trace)
