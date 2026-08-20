from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
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
    rule = RecurrenceRule(time_of_day=time(9, 0), timezone="UTC", days_of_week=[0, 4])
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


def test_time_of_day_inside_spring_forward_gap_shifts_forward_once() -> None:
    # Europe/Madrid 2026-03-29: 02:00 -> 03:00 local, so 02:30 does not
    # exist that day. Policy: resolves to the first valid instant after
    # the gap (03:30 local), exactly once.
    zone = ZoneInfo("Europe/Madrid")
    rule = RecurrenceRule(time_of_day=time(2, 30), timezone="Europe/Madrid")
    after = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)

    occurrence = next_occurrence(rule, after)

    local = occurrence.astimezone(zone)
    assert local.date().isoformat() == "2026-03-29"
    assert (local.hour, local.minute) == (3, 30)


def test_time_of_day_inside_spring_forward_gap_sequence_is_strictly_increasing() -> None:
    zone = ZoneInfo("Europe/Madrid")
    rule = RecurrenceRule(time_of_day=time(2, 30), timezone="Europe/Madrid")
    after = datetime(2026, 3, 27, 23, 0, tzinfo=UTC)

    occurrences = []
    for _ in range(4):
        occurrence = next_occurrence(rule, after)
        occurrences.append(occurrence)
        after = occurrence

    assert occurrences == sorted(set(occurrences))
    transition_day = [
        occ for occ in occurrences if occ.astimezone(zone).date().isoformat() == "2026-03-29"
    ]
    assert len(transition_day) == 1


def test_time_of_day_inside_fall_back_overlap_resolves_to_earlier_instant() -> None:
    # Europe/Madrid 2026-10-25: 03:00 -> 02:00 local, so 02:30 occurs
    # twice. Policy: resolves to the earlier (pre-transition) instant,
    # exactly once.
    zone = ZoneInfo("Europe/Madrid")
    rule = RecurrenceRule(time_of_day=time(2, 30), timezone="Europe/Madrid")
    after = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)

    occurrence = next_occurrence(rule, after)

    local = occurrence.astimezone(zone)
    assert local.date().isoformat() == "2026-10-25"
    assert (local.hour, local.minute) == (2, 30)
    assert local.utcoffset() == timedelta(hours=2)  # CEST, the earlier instance


def test_time_of_day_inside_fall_back_overlap_sequence_is_strictly_increasing() -> None:
    zone = ZoneInfo("Europe/Madrid")
    rule = RecurrenceRule(time_of_day=time(2, 30), timezone="Europe/Madrid")
    after = datetime(2026, 10, 23, 23, 0, tzinfo=UTC)

    occurrences = []
    for _ in range(4):
        occurrence = next_occurrence(rule, after)
        occurrences.append(occurrence)
        after = occurrence

    assert occurrences == sorted(set(occurrences))
    transition_day = [
        occ for occ in occurrences if occ.astimezone(zone).date().isoformat() == "2026-10-25"
    ]
    assert len(transition_day) == 1
