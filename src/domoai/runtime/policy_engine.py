"""Central policy and risk evaluation."""

from __future__ import annotations

import hashlib
import json

from domoai.config.policy_loader import choose_policy
from domoai.domain.models import Command, Device, Policy, PolicyAction, PolicyDecision, RiskClass


class PolicyEngine:
    def __init__(self, policies: list[Policy]) -> None:
        self.policies = policies

    @property
    def revision(self) -> str:
        payload = [
            policy.model_dump(mode="json")
            for policy in sorted(self.policies, key=lambda item: item.id)
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return f"policy-{hashlib.sha256(encoded).hexdigest()[:16]}"

    def evaluate(self, command: Command, device: Device, capability: str) -> PolicyDecision:
        decision = choose_policy(
            self.policies,
            device_id=device.id,
            area_id=device.area_id,
            capability=capability,
            value=command.value,
            risk_class=command.risk_class.value,
        )
        if decision.action is PolicyAction.ALLOW and command.risk_class is not RiskClass.SAFE:
            return PolicyDecision(
                policy_id=decision.policy_id,
                action=PolicyAction.CONFIRM,
                reason=f"Risk class {command.risk_class.value} requires operator confirmation",
            )
        return decision
