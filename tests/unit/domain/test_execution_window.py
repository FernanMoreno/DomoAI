from datetime import UTC, datetime

import pytest

from domoai.domain.models import ExecutionWindow


def test_execution_window_canonical_digest_keeps_timezone_identity_and_instant() -> None:
    summer = ExecutionWindow(
        intended_at=datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
        not_before=datetime(2026, 10, 25, 1, 29, tzinfo=UTC),
        not_after=datetime(2026, 10, 25, 1, 31, tzinfo=UTC),
        timezone="Europe/Madrid",
        revision=4,
    )
    same_instant_different_zone = summer.model_copy(update={"timezone": "UTC"})

    assert summer.digest != same_instant_different_zone.digest
    assert summer.canonical_payload()["intended_at"] == "2026-10-25T01:30:00Z"


@pytest.mark.parametrize(
    "field",
    ["not_before", "not_after"],
)
def test_execution_window_rejects_invalid_order(field: str) -> None:
    values = {
        "intended_at": datetime(2026, 8, 23, 13, tzinfo=UTC),
        "not_before": datetime(2026, 8, 23, 12, tzinfo=UTC),
        "not_after": datetime(2026, 8, 23, 14, tzinfo=UTC),
        "timezone": "Europe/Madrid",
    }
    values[field] = datetime(2026, 8, 23, 14, tzinfo=UTC)
    if field == "not_after":
        values[field] = datetime(2026, 8, 23, 12, tzinfo=UTC)

    with pytest.raises(ValueError, match="execution window"):
        ExecutionWindow(**values)
