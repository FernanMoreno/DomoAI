"""Single freshness decision boundary for physical preconditions."""

from __future__ import annotations

from dataclasses import dataclass
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

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()

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
        age_seconds = max(0.0, (self.clock.now() - snapshot.observed_at).total_seconds())
        if snapshot.value != precondition.expected:
            return FreshnessDecision(
                False,
                "value_mismatch",
                snapshot,
                age_seconds=age_seconds,
                source_revision=source_revision,
            )
        if snapshot.status is StateStatus.CURRENT:
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
            "precondition_state_not_current",
            snapshot,
            age_seconds=age_seconds,
            source_revision=source_revision,
        )
