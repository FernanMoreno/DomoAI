"""Load and deterministically select local policy rules."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from domoai.domain.models import Policy, PolicyAction, PolicyDecision


def load_policies(payload: Mapping[str, Any] | list[Mapping[str, Any]]) -> list[Policy]:
    raw_policies = payload if isinstance(payload, list) else payload.get("policies", [])
    policies = [Policy.model_validate(raw_policy) for raw_policy in raw_policies]
    return sorted(
        (policy for policy in policies if policy.enabled),
        key=lambda policy: (-policy.priority, policy.id),
    )


def load_policy_file(path: Path) -> list[Policy]:
    with path.open("rb") as file:
        return load_policies(tomllib.load(file))


def choose_policy(
    policies: list[Policy],
    *,
    device_id: str,
    area_id: str | None,
    capability: str | None = None,
    value: Any = None,
    risk_class: str | None = None,
) -> PolicyDecision:
    context = {
        "device_id": device_id,
        "area_id": area_id,
        "capability": capability,
        "value": value,
        "risk_class": risk_class,
    }
    for policy in policies:
        if _matches(policy.target, context) and _matches_conditions(policy.conditions, context):
            return PolicyDecision(
                policy_id=policy.id,
                action=policy.action,
                reason=f"Matched policy {policy.id}",
            )
    return PolicyDecision(
        action=PolicyAction.ALLOW,
        reason="No matching policy; default allow applies",
    )


def _matches(target: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    return all(expected is None or context.get(key) == expected for key, expected in target.items())


def _matches_conditions(conditions: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
    for key, expected in conditions.items():
        if key in {"min_value", "value_min"}:
            actual = context.get("value")
            if actual is None or not isinstance(actual, (int, float)) or actual < expected:
                return False
        elif key in {"max_value", "value_max"}:
            actual = context.get("value")
            if actual is None or not isinstance(actual, (int, float)) or actual > expected:
                return False
        elif key in {"value_in", "allowed_values"}:
            actual = context.get("value")
            if actual not in expected:
                return False
        elif context.get(key) != expected:
            return False
    return True
