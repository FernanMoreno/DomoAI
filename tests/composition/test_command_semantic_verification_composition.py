from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.application.executor import PlanExecutor
from domoai.domain.models import (
    Command,
    CommandPostcondition,
    SourceRef,
    StateSnapshot,
    StateStatus,
)


def _state(value: object) -> StateSnapshot:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    return StateSnapshot(
        device_id="light-1",
        capability="power",
        value=value,
        observed_at=now,
        received_at=now,
        status=StateStatus.CURRENT,
        source_ref=SourceRef(adapter_id="fixture", external_id="light-1"),
    )


@pytest.mark.composition
def test_toggle_requires_declared_transition_and_stop_never_confirms_snapshot_only() -> None:
    toggle = Command(
        id="toggle-1",
        device_id="light-1",
        command="toggle",
        idempotency_key="toggle-1-key",
        postconditions=[
            CommandPostcondition(
                capability="power",
                verification="toggle_transition",
            )
        ],
    )
    stop = Command(
        id="stop-1",
        device_id="light-1",
        command="stop",
        idempotency_key="stop-1-key",
        postconditions=[
            CommandPostcondition(
                capability="power",
                verification="unconfirmed",
            )
        ],
    )

    assert PlanExecutor._postcondition_matches(
        toggle, "power", _state(False), before_state=_state(True)
    )
    assert not PlanExecutor._postcondition_matches(
        toggle, "power", _state(True), before_state=_state(True)
    )
    assert not PlanExecutor._postcondition_matches(stop, "power", _state(False))
