"""Central policy and risk evaluation."""

from __future__ import annotations

import hashlib
import json

from domoai.config.policy_loader import choose_policy
from domoai.domain.models import Command, Device, Policy, PolicyAction, PolicyDecision, RiskClass
from domoai.runtime.risk_classifier import RiskClassifier

_RISK_ORDER = {RiskClass.SAFE: 0, RiskClass.CONFIRM: 1, RiskClass.RESTRICTED: 2}


class PolicyEngine:
    def __init__(
        self, policies: list[Policy], risk_classifier: RiskClassifier | None = None
    ) -> None:
        self.policies = policies
        self.risk_classifier = risk_classifier or RiskClassifier()

    @property
    def revision(self) -> str:
        payload = [
            policy.model_dump(mode="json")
            for policy in sorted(self.policies, key=lambda item: item.id)
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"policy-{hashlib.sha256(encoded).hexdigest()[:16]}"

    def evaluate(self, command: Command, device: Device, capability: str) -> PolicyDecision:
        classified = self.risk_classifier.classify(device, capability, command)
        effective_risk = (
            classified
            if _RISK_ORDER[classified] >= _RISK_ORDER[command.risk_class]
            else command.risk_class
        )
        decision = choose_policy(
            self.policies,
            device_id=device.id,
            area_id=device.area_id,
            capability=capability,
            value=command.value,
            risk_class=effective_risk.value,
        )
        if decision.action is PolicyAction.ALLOW and effective_risk is not RiskClass.SAFE:
            return PolicyDecision(
                policy_id=decision.policy_id,
                action=PolicyAction.CONFIRM,
                reason=f"Risk class {effective_risk.value} requires operator confirmation",
            )
        return decision
