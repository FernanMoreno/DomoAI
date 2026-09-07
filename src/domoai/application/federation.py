"""Local validation boundary for federated control proposals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domoai.application.authority import AuthorityPolicy
from domoai.domain.federation import (
    FederationIntent,
    FederationIntentStatus,
    verify_federation_signature,
)
from domoai.runtime.clock import Clock


@dataclass(frozen=True)
class FederationResult:
    intent_id: str
    status: FederationIntentStatus
    target_household_id: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": "v1",
            "intent_id": self.intent_id,
            "status": self.status.value,
            "target_household_id": self.target_household_id,
            "reason": self.reason,
        }


class FederationBoundary:
    """Accept proposals for this home without ever dispatching them."""

    _OPERATIONS = frozenset({"execute_plan", "schedule_plan", "cancel_scheduled_plan"})

    def __init__(
        self,
        policy: AuthorityPolicy,
        *,
        shared_secret: bytes,
        clock: Clock | None = None,
    ) -> None:
        self.policy = policy
        self.shared_secret = shared_secret
        self.clock = clock
        self._accepted: dict[str, FederationIntent] = {}
        self._idempotency: dict[str, str] = {}

    def submit(self, intent: FederationIntent) -> FederationResult:
        now = self._now()
        if intent.target_tenant_id != self.policy.tenant_id:
            return self._reject(intent, "target tenant is not this deployment")
        if intent.target_household_id != self.policy.household_id:
            return self._reject(intent, "target household is not this deployment")
        if intent.operation not in self._OPERATIONS:
            return self._reject(intent, "federated operation is not allowlisted")
        if intent.expires_at <= now:
            return self._reject(intent, "federated intent has expired")
        if not verify_federation_signature(intent, self.shared_secret):
            return self._reject(intent, "federated signature is invalid")
        existing_intent = self._accepted.get(intent.intent_id)
        if (
            existing_intent is not None
            and existing_intent.signing_payload() != intent.signing_payload()
        ):
            return self._reject(intent, "federated intent ID conflicts")
        previous = self._idempotency.get(intent.idempotency_key)
        if previous is not None:
            previous_intent = self._accepted.get(previous)
            if previous == intent.intent_id and previous_intent is not None:
                if previous_intent.signing_payload() != intent.signing_payload():
                    return self._reject(intent, "federated idempotency key conflicts")
                return FederationResult(
                    intent.intent_id,
                    FederationIntentStatus.ACCEPTED,
                    intent.target_household_id,
                    "duplicate proposal replayed idempotently",
                )
            return self._reject(intent, "federated idempotency key conflicts")

        # Local service policy is checked as a target-side gate. This is not
        # approval of the source principal and does not call an adapter.
        self.policy.authorize(
            self.policy.local_context(),
            operation=intent.operation,
            target_household_id=intent.target_household_id,
        )
        self._accepted[intent.intent_id] = intent
        self._idempotency[intent.idempotency_key] = intent.intent_id
        return FederationResult(
            intent.intent_id,
            FederationIntentStatus.ACCEPTED,
            intent.target_household_id,
            "proposal accepted for local policy and explicit local admission",
        )

    def complete(self, intent_id: str) -> FederationResult:
        intent = self._accepted.get(intent_id)
        if intent is None:
            return FederationResult(
                intent_id,
                FederationIntentStatus.UNKNOWN,
                self.policy.household_id,
                "unknown intent",
            )
        return FederationResult(
            intent_id,
            FederationIntentStatus.COMPLETED,
            intent.target_household_id,
            "completion recorded without remote actuation",
        )

    def _reject(self, intent: FederationIntent, reason: str) -> FederationResult:
        return FederationResult(
            intent.intent_id,
            FederationIntentStatus.REJECTED,
            intent.target_household_id,
            reason,
        )

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock.now()
        return datetime.now(UTC)
