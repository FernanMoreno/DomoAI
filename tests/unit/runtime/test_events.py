from domoai.domain.models import AuditEvent
from domoai.runtime.events import AuditLog, redact_payload


def test_redaction_removes_credentials_recursively() -> None:
    payload = {
        "token": "secret",
        "nested": {"authorization": "Bearer secret", "value": 42},
        "items": [{"password": "secret"}],
    }

    assert redact_payload(payload) == {
        "token": "[REDACTED]",
        "nested": {"authorization": "[REDACTED]", "value": 42},
        "items": [{"password": "[REDACTED]"}],
    }


def test_in_memory_events_stay_capped_at_the_default_and_evict_oldest_first() -> None:
    audit = AuditLog(max_events=5)

    for index in range(8):
        audit.append(
            event_type="test_event",
            actor="test",
            subject_id=str(index),
            payload={},
        )

    assert len(audit.events) == 5
    assert [event.subject_id for event in audit.events] == ["3", "4", "5", "6", "7"]


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append_event(self, event: AuditEvent) -> None:
        self.events.append(event)


def test_evicted_events_still_reached_the_sink_exactly_once() -> None:
    sink = _RecordingSink()
    audit = AuditLog(sink=sink, max_events=3)

    for index in range(10):
        audit.append(
            event_type="test_event",
            actor="test",
            subject_id=str(index),
            payload={},
        )

    assert len(audit.events) == 3
    assert [event.subject_id for event in sink.events] == [str(index) for index in range(10)]


class _FailingSink:
    def __init__(self) -> None:
        self.attempts = 0

    def append_event(self, event: AuditEvent) -> None:
        self.attempts += 1
        raise RuntimeError("storage boundary overloaded")


def test_sink_failure_never_propagates_to_the_caller() -> None:
    # Closes P1.6 (2026-08-24 re-audit): a durable-sink failure (e.g. the
    # SQLite storage boundary rejecting admission under load) must degrade
    # audit to memory-only retention, not raise into whatever plan/execution
    # lifecycle code called audit.append().
    sink = _FailingSink()
    audit = AuditLog(sink=sink, max_events=5)

    event = audit.append(
        event_type="plan_execution_started",
        actor="runtime",
        subject_id="plan-1",
        payload={},
    )

    assert event in audit.events
    assert sink.attempts == 1
    assert audit.sink_failure_count == 1
    assert "storage boundary overloaded" in (audit.last_sink_error or "")


def test_negative_indexing_and_slicing_work_within_the_retained_window() -> None:
    audit = AuditLog(max_events=1000)

    for index in range(3):
        audit.append(
            event_type="test_event",
            actor="test",
            subject_id=str(index),
            payload={},
        )

    assert audit.events[-1].subject_id == "2"
    assert [event.subject_id for event in audit.events[:2]] == ["0", "1"]


def test_default_cap_is_around_one_thousand() -> None:
    audit = AuditLog()

    for index in range(1500):
        audit.append(
            event_type="test_event",
            actor="test",
            subject_id=str(index),
            payload={},
        )

    assert len(audit.events) == 1000


def test_append_stamps_created_at_from_the_injected_clock() -> None:
    from datetime import UTC, datetime

    from domoai.runtime.clock import FixedClock

    fixed = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    audit = AuditLog(clock=fixed)

    event = audit.append(event_type="test_event", actor="test", subject_id="x", payload={})

    assert event.created_at == fixed.now()
