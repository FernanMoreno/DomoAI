"""DST-correct computation of a recurring schedule's next occurrence."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from domoai.domain.models import RecurrenceRule

_MAX_DAYS_SEARCHED = 8


def recurrence_digest(plan_id: str, rule: RecurrenceRule) -> str:
    payload = {
        "schema": "standing-automation-v1",
        "plan_id": plan_id,
        "rule": rule.model_dump(mode="json"),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def next_occurrence(rule: RecurrenceRule, after: datetime) -> datetime:
    """First occurrence strictly after `after`, in UTC.

    Computed entirely in local (zoneinfo) time and converted to UTC only
    as the final step, so a fixed local time (e.g. 22:00) survives a DST
    transition instead of silently drifting by an hour.

    DST transition policy (PEP 495 fold=0, explicit and deliberate):
    - Spring-forward (nonexistent local time, e.g. 02:30 during a
      02:00->03:00 jump): resolves to the first valid local instant
      after the gap (e.g. 03:30) -- the schedule still fires exactly
      once that day, just shifted forward by the gap size.
    - Fall-back (ambiguous local time, occurring twice, e.g. 02:30
      during a 03:00->02:00 step-back): resolves to the earlier
      (pre-transition) occurrence -- the schedule fires exactly once
      on the 25-hour day, not twice.
    Both properties are verified by tests exercising time_of_day values
    that land exactly inside a transition hour.
    """
    zone = ZoneInfo(rule.timezone)
    local_after = after.astimezone(zone)
    candidate_date = local_after.date()
    for _ in range(_MAX_DAYS_SEARCHED):
        if rule.days_of_week is None or candidate_date.weekday() in rule.days_of_week:
            candidate = datetime.combine(candidate_date, rule.time_of_day, tzinfo=zone).replace(
                fold=0
            )
            round_trip = candidate.astimezone(UTC).astimezone(zone)
            if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
                # ZoneInfo represents a spring-forward gap with a synthetic
                # offset. Use the first real local instant after that gap
                # before comparing with `after`; otherwise a rule created at
                # 03:10 would incorrectly skip today's 02:30 -> 03:30 run.
                candidate = round_trip
            if candidate > local_after:
                return candidate.astimezone(UTC)
        candidate_date = candidate_date + timedelta(days=1)
    raise ValueError("No matching occurrence found within the search window")
