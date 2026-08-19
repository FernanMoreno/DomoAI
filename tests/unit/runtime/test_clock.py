from datetime import UTC, datetime, timedelta

from domoai.runtime.clock import FixedClock, SystemClock


def test_system_clock_reflects_real_time() -> None:
    before = datetime.now(UTC)
    reported = SystemClock().now()
    after = datetime.now(UTC)

    assert reported.tzinfo is not None
    assert before <= reported <= after


def test_fixed_clock_returns_exactly_what_was_set() -> None:
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)

    assert clock.now() == initial

    advanced = initial + timedelta(hours=1)
    clock.set(advanced)

    assert clock.now() == advanced
