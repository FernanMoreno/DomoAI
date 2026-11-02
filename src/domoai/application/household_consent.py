"""Pure projection of already-authorized runtime data, without approval side effects.

Callers supply an authorized target, inventory and complete preference snapshot.
This function does not authenticate, issue grants, or dispatch.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import NoReturn

from domoai.domain.automation import AutomationRule
from domoai.domain.household_consent import (
    ConsentAction,
    ConsentBindings,
    ConsentConstraints,
    ConsentControlVersion,
    ConsentEvidence,
    ConsentHorizon,
    ConsentReference,
    ConsentRisk,
    ConsentView,
    ConsentWindow,
)
from domoai.domain.household_preferences import PreferenceProfile
from domoai.domain.models import CapabilityKind, Command, Device, Plan


def _clarify(reason: str) -> NoReturn:
    raise ValueError(f"clarification_required: {reason}")


def _action(command: Command, devices: Sequence[Device]) -> ConsentAction:
    matches = [device for device in devices if device.id == command.device_id]
    if len(matches) != 1:
        _clarify("an exact unique device id is required")
    device = matches[0]
    capabilities = [
        capability
        for capability in device.capabilities
        if capability.writable and command.command in capability.commands
    ]
    if len(capabilities) != 1:
        _clarify("command capability is not unambiguous")
    capability = capabilities[0]
    if command.unit != capability.unit:
        _clarify("command unit does not match the capability")
    value = command.value
    if capability.kind in {CapabilityKind.NUMBER, CapabilityKind.INTEGER}:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            _clarify("numeric command requires a finite numeric value")
        if capability.kind is CapabilityKind.INTEGER and not isinstance(value, int):
            _clarify("integer capability requires an integer")
        if capability.minimum is not None and value < capability.minimum:
            _clarify("command value is below capability bounds")
        if capability.maximum is not None and value > capability.maximum:
            _clarify("command value is above capability bounds")
    return ConsentAction.model_validate(
        {
            "command_id": command.id,
            "device_id": device.id,
            "device_name": device.name,
            "command": command.command,
            "value": value,
            "unit": command.unit,
            "risk_class": command.risk_class.value,
        }
    )


def build_consent_view(
    *,
    target: Plan | AutomationRule,
    devices: Sequence[Device],
    preferences: PreferenceProfile,
    control_version: ConsentControlVersion | dict[str, object],
    now: datetime,
    timezone: str,
) -> ConsentView:
    """Describe the exact plan/rule; unresolved inputs require another review."""
    if now.utcoffset() is None:
        _clarify("review clock must have an offset")
    now = now.astimezone(UTC)
    if preferences.next_cursor is not None:
        _clarify("a complete preference snapshot is required")
    rule = target if isinstance(target, AutomationRule) else None
    plan = target.plan_template if isinstance(target, AutomationRule) else target
    constraints = None
    validation_digest = None
    window_digest = None
    rule_digest = None
    evidence = []
    if rule is not None:
        if rule.expires_at is None or rule.expires_at <= now:
            _clarify("rule authorization needs an explicit future expiry")
        expires_at = rule.expires_at
        start, end = now, expires_at
        rule_digest = rule.definition_digest
        if rule_digest is None:
            _clarify("rule definition evidence is missing")
        constraints = ConsentConstraints(
            scope=rule.scope,
            trigger=rule.trigger,
            conditions=rule.conditions,
            cooldown_seconds=rule.cooldown_seconds,
        )
        evidence.append(ConsentEvidence(kind="rule", id=rule.id, digest=rule_digest))
    else:
        if plan.execution_window is None:
            _clarify("an explicit execution window is required")
        window = plan.execution_window
        if window.timezone != timezone:
            _clarify("review timezone differs from the execution window")
        validation = plan.validation
        if validation is None or validation.valid_until is None or validation.valid_until <= now:
            _clarify("current validation evidence is required")
        if plan.expires_at is None or plan.expires_at <= now:
            _clarify("plan expiry is required")
        start, end = window.not_before, window.not_after
        expires_at = min(plan.expires_at, validation.valid_until)
        validation_digest = validation.digest
        window_digest = window.digest
        evidence.append(ConsentEvidence(kind="validation", id=plan.id, digest=validation_digest))
    try:
        review_window = ConsentWindow(not_before=start, not_after=end, timezone=timezone)
    except ValueError as error:
        _clarify(str(error))
    authority = target.authority
    return ConsentView(
        schema_version="domoai.household-consent.v1",
        reference=ConsentReference(kind="rule" if rule is not None else "plan", id=target.id),
        tenant_id=authority.tenant_id,
        household_id=authority.household_id,
        actions=[_action(command, devices) for command in plan.commands],
        window=review_window,
        duration_seconds=int((review_window.not_after - review_window.not_before).total_seconds()),
        expires_at=expires_at,
        constraints=constraints,
        risks=[
            ConsentRisk.model_validate(decision.model_dump(mode="json"))
            for decision in plan.policy_decisions
        ],
        evidence_refs=evidence,
        bindings=ConsentBindings(
            validation_digest=validation_digest,
            window_digest=window_digest,
            bundle_digest=None,
            recurrence_digest=None,
            rule_digest=rule_digest,
            schedule_revision=plan.schedule_revision,
            preference_version=preferences.version,
            preference_observed_at=preferences.observed_at,
            preference_next_transition_at=preferences.next_transition_at,
            preference_horizon=ConsentHorizon(
                start=review_window.not_before, end=review_window.not_after
            ),
            control_version=ConsentControlVersion.model_validate(control_version),
        ),
        permission_status="awaiting_review",
        physical_effect_refs=[],
    )
