"""Single freshness decision boundary for physical preconditions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from domoai.domain.models import PolicyDecision, Precondition, StateSnapshot, StateStatus
from domoai.runtime.clock import Clock, SystemClock


@dataclass(frozen=True)
class FreshnessDecision:
    """Serializable-safe evidence for one precondition decision."""

    satisfied: bool
    reason_code: str
    snapshot: StateSnapshot | None
    stale_exception: bool = False
    age_seconds: float | None = None
    source_revision: int | None = None

    def details(self) -> dict[str, Any]:
        if self.snapshot is None:
            return {"reason_code": self.reason_code, "status": None}
        return {
            "reason_code": self.reason_code,
            "status": self.snapshot.status.value,
            "observed_at": self.snapshot.observed_at.isoformat(),
            "received_at": self.snapshot.received_at.isoformat(),
            "age_seconds": self.age_seconds,
            "source_revision": self.source_revision,
            "source_ref": self.snapshot.source_ref.model_dump(mode="json"),
            "stale_exception": self.stale_exception,
        }


class FreshnessEvaluator:
    """Evaluate value and evidence status without contacting an adapter."""

    def __init__(
        self,
        clock: Clock | None = None,
        *,
        max_age: timedelta = timedelta(minutes=5),
    ) -> None:
        self.clock = clock or SystemClock()
        if max_age.total_seconds() <= 0:
            raise ValueError("freshness max_age must be positive")
        self.max_age = max_age

    def evaluate(
        self,
        snapshot: StateSnapshot | None,
        precondition: Precondition,
        policy_decision: PolicyDecision | None = None,
        source_revision: int | None = None,
    ) -> FreshnessDecision:
        if snapshot is None:
            return FreshnessDecision(
                False,
                "evidence_missing",
                None,
                source_revision=source_revision,
            )
        now = self.clock.now()
        age_seconds = (now - snapshot.received_at).total_seconds()
        if snapshot.status is StateStatus.UNAVAILABLE:
            return FreshnessDecision(
                False,
                "evidence_unavailable",
                snapshot,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        if snapshot.status is StateStatus.INVALID:
            return FreshnessDecision(
                False,
                "evidence_invalid",
                snapshot,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        if age_seconds < 0 or snapshot.observed_at > now:
            return FreshnessDecision(
                False,
                "future_observation",
                snapshot,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        if snapshot.value != precondition.expected:
            return FreshnessDecision(
                False,
                "value_mismatch",
                snapshot,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        if snapshot.status is StateStatus.CURRENT:
            if age_seconds > self.max_age.total_seconds():
                return FreshnessDecision(
                    False,
                    "current_evidence_expired",
                    snapshot,
                    age_seconds=age_seconds,
                    source_revision=source_revision,
                )
            return FreshnessDecision(
                True,
                "current_evidence",
                snapshot,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        if (
            snapshot.status is StateStatus.STALE
            and precondition.allow_stale
            and policy_decision is not None
            and policy_decision.allows_stale
            and policy_decision.action.value in {"allow", "confirm"}
        ):
            return FreshnessDecision(
                True,
                "stale_evidence_explicitly_allowed",
                snapshot,
                stale_exception=True,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        return FreshnessDecision(
            False,
            "stale_evidence_not_allowed",
            snapshot,
            age_seconds=age_seconds,
            source_revision=source_revision,
        )
