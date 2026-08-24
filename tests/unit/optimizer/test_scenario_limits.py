"""Size bounds on OptimizationScenario's agent-facing list fields (P2.2 from
the 2026-08-24 re-audit of commit 61439f3).

`OptimizationScenario` is parsed from an untrusted MCP caller before the
solve ever reaches the bounded `OptimizationWorker` (P2.1) -- Pydantic
parsing, `validate_scenario`, and registry lookups all run inline on the
event loop first, so unbounded lists here are a resource-exhaustion vector
independent of the solver's own time limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.optimizer.horizon import Horizon
from domoai.optimizer.scenario import Load, OptimizationScenario


def _horizon() -> Horizon:
    start = datetime(2026, 8, 24, 12, tzinfo=UTC)
    return Horizon(
        start=start,
        end=start + timedelta(minutes=15),
        resolution_minutes=15,
        timezone="Europe/Madrid",
    )


def _load(index: int) -> Load:
    return Load(
        id=f"load-{index}",
        device_id="fixture.device",
        capability="power",
        command="turn_on",
    )


def test_loads_within_the_limit_are_accepted() -> None:
    OptimizationScenario(
        id="within-limit", horizon=_horizon(), loads=[_load(i) for i in range(100)]
    )


def test_loads_beyond_the_limit_are_rejected() -> None:
    with pytest.raises(ValidationError):
        OptimizationScenario(
            id="over-limit", horizon=_horizon(), loads=[_load(i) for i in range(101)]
        )


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("loads", 100),
        ("ev_loads", 16),
        ("comfort_loads", 32),
        ("constraints", 64),
        ("objectives", 16),
        ("inputs", 64),
    ],
)
def test_each_list_field_has_a_bound(field: str, limit: int) -> None:
    schema = OptimizationScenario.model_fields[field]
    max_length = next(
        (item.max_length for item in schema.metadata if hasattr(item, "max_length")), None
    )
    assert max_length == limit
