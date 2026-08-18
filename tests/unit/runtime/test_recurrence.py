from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from domoai.domain.models import RecurrenceRule
from domoai.runtime.recurrence import next_occurrence


def test_next_occurrence_survives_spring_forward() -> None:
    # Europe/Madrid spring-forward 2026: clocks jump 02:00 -> 03:00 local
    # on 2026-03-29. A rule fixed at 22:00 local must still land at 22:00
    # local on that date, not silently shift in UTC.
    rule = RecurrenceRule(time_of_day=time(22, 0), timezone="Europe/Madrid")
    after = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)

    occurrence = next_occurrence(rule, after)

    local = occurrence.astimezone(ZoneInfo("Europe/Madrid"))
    assert local.date().isoformat() == "2026-03-29"
    assert (local.hour, local.minute) == (22, 0)


def test_next_occurrence_survives_fall_back() -> None:
    # Europe/Madrid fall-back 2026: clocks step back 03:00 -> 02:00 local
    # on 2026-10-25.
    rule = RecurrenceRule(time_of_day=time(22, 0), timezone="Europe/Madrid")
    after = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)

    occurrence = next_occurrence(rule, after)

    local = occurrence.astimezone(ZoneInfo("Europe/Madrid"))
    assert local.date().isoformat() == "2026-10-25"
    assert (local.hour, local.minute) == (22, 0)


def test_days_of_week_restriction_is_honored() -> None:
    # 2026-08-19 is a Wednesday (weekday()==2). Restrict to Mon/Fri (0, 4).
    rule = RecurrenceRule(
        time_of_day=time(9, 0), timezone="UTC", days_of_week=[0, 4]
    )
    after = datetime(2026, 8, 19, 0, 0, tzinfo=UTC)

    occurrence = next_occurrence(rule, after)

    assert occurrence.weekday() in (0, 4)
    assert occurrence.date().isoformat() == "2026-08-21"  # next Friday


def test_next_occurrence_is_always_strictly_after() -> None:
    rule = RecurrenceRule(time_of_day=time(12, 0), timezone="UTC")
    after = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    occurrence = next_occurrence(rule, after)

    assert occurrence > after
    assert occurrence.date().isoformat() == "2026-08-20"
