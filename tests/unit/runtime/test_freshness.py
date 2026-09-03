from datetime import UTC, datetime, timedelta

from domoai.domain.models import (
    PolicyAction,
    PolicyDecision,
    Precondition,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.freshness import FreshnessEvaluator


def _snapshot(status: StateStatus) -> StateSnapshot:
    observed_at = datetime(2026, 8, 23, 10, tzinfo=UTC)
    return StateSnapshot(
        device_id="garage.door",
        capability="door_closed",
        value=True,
        observed_at=observed_at,
        received_at=observed_at,
        status=status,
        source_ref=SourceRef(adapter_id="fixture", external_id="garage.door"),
    )


def _precondition(*, allow_stale: bool = False) -> Precondition:
    return Precondition(
        device_id="garage.door",
        capability="door_closed",
        expected=True,
        allow_stale=allow_stale,
    )


def test_current_matching_evidence_is_authorized() -> None:
    evaluator = FreshnessEvaluator(FixedClock(datetime(2026, 8, 23, 10, 1, tzinfo=UTC)))

    decision = evaluator.evaluate(_snapshot(StateStatus.CURRENT), _precondition())

    assert decision.satisfied is True
    assert decision.reason_code == "current_evidence"


def test_current_evidence_expires_by_server_owned_age() -> None:
    evaluator = FreshnessEvaluator(FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))

    decision = evaluator.evaluate(_snapshot(StateStatus.CURRENT), _precondition())

    assert decision.satisfied is False
    assert decision.reason_code == "current_evidence_expired"


def test_current_evidence_uses_latest_runtime_receipt_without_erasing_source_age() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    evaluator = FreshnessEvaluator(FixedClock(now))
    snapshot = _snapshot(StateStatus.CURRENT).model_copy(
        update={"received_at": now - timedelta(seconds=30)}
    )

    decision = evaluator.evaluate(snapshot, _precondition())

    assert decision.satisfied is True
    assert decision.age_seconds == 30
    assert decision.snapshot is not None
    assert decision.snapshot.observed_at == datetime(2026, 8, 23, 10, tzinfo=UTC)


def test_missing_evidence_fails_closed() -> None:
    decision = FreshnessEvaluator().evaluate(None, _precondition())

    assert decision.satisfied is False
    assert decision.reason_code == "evidence_missing"


def test_stale_matching_evidence_requires_explicit_policy() -> None:
    evaluator = FreshnessEvaluator(FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))
    policy = PolicyDecision(
        action=PolicyAction.ALLOW,
        reason="explicit stale policy",
        policy_id="policy-stale",
        allows_stale=True,
    )

    rejected = evaluator.evaluate(_snapshot(StateStatus.STALE), _precondition(allow_stale=True))
    allowed = evaluator.evaluate(
        _snapshot(StateStatus.STALE), _precondition(allow_stale=True), policy
    )

    assert rejected.satisfied is False
    assert allowed.satisfied is True
    assert allowed.stale_exception is True
    assert allowed.details()["age_seconds"] == timedelta(hours=2).total_seconds()


def test_unavailable_and_invalid_evidence_never_become_authorizable() -> None:
    evaluator = FreshnessEvaluator(FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))
    policy = PolicyDecision(
        action=PolicyAction.ALLOW,
        reason="explicit stale policy",
        allows_stale=True,
    )

    for status, reason_code in (
        (StateStatus.UNAVAILABLE, "evidence_unavailable"),
        (StateStatus.INVALID, "evidence_invalid"),
    ):
        decision = evaluator.evaluate(_snapshot(status), _precondition(allow_stale=True), policy)
        assert decision.satisfied is False
        assert decision.reason_code == reason_code


def test_stale_evidence_without_explicit_policy_has_distinct_reason() -> None:
    evaluator = FreshnessEvaluator(FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC)))
    decision = evaluator.evaluate(_snapshot(StateStatus.STALE), _precondition(allow_stale=True))

    assert decision.satisfied is False
    assert decision.reason_code == "stale_evidence_not_allowed"


def test_future_observation_is_rejected_instead_of_being_treated_as_fresh() -> None:
    now = datetime(2026, 8, 23, 10, tzinfo=UTC)
    evaluator = FreshnessEvaluator(FixedClock(now))
    future = _snapshot(StateStatus.CURRENT).model_copy(
        update={
            "observed_at": now + timedelta(minutes=1),
            "received_at": now + timedelta(minutes=1),
        }
    )

    decision = evaluator.evaluate(future, _precondition())

    assert decision.satisfied is False
    assert decision.reason_code == "future_observation"
    assert decision.age_seconds == -60
