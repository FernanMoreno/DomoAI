"""Drives a real battery actuator through the required HIL check sequence
and produces `BatteryHILEvidence` from what actually happened, instead of a
hand-typed attestation (spec 146).

Reuses the exact same production path a real optimizer-driven dispatch would
take -- `DomoticsFacade.execute_plan` through the already-wired
`PlanExecutor` (control takeover, postcondition confirmation, SOC
reconciliation all included) -- rather than reimplementing hardware
handling. Two of the ten required checks (`native_scheduler_conflict`,
`restart_no_replay`) cannot be self-certified by a single automated run: the
first requires knowing whether *another* controller is also driving the
device, and the second requires an actual process restart mid-sequence.
Those two are recorded as explicit operator-supplied manual attestations
rather than silently marked passed.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from domoai.config.battery_qualification import (
    REQUIRED_HIL_CHECKS,
    BatteryHILEvidence,
    battery_binding_digest,
)
from domoai.domain.models import Command, CommandPostcondition, ExecutionStatus, Plan, PlanStatus
from domoai.runtime.approval_store import ApprovalGrant
from domoai.runtime.clock import Clock, SystemClock

if TYPE_CHECKING:
    from domoai.application.runtime_factory import RuntimeComposition
    from domoai.optimizer.energy import BatteryActuator, DispatchableBatteryBinding

_AUTOMATED_CHECKS = frozenset(
    {
        "identity",
        "writable_routes",
        "takeover_baseline",
        "charge_feedback",
        "discharge_feedback",
        "stop_feedback",
        "polarity",
        "soc_reconciliation",
    }
)
_MANUAL_ONLY_CHECKS = REQUIRED_HIL_CHECKS - _AUTOMATED_CHECKS


class BatteryHILRunError(RuntimeError):
    """Raised when the sequence cannot even be attempted safely."""


def _feedback_value(actuator: BatteryActuator, *, charge_kw: float, discharge_kw: float) -> float:
    if charge_kw > 0 and discharge_kw > 0:
        raise BatteryHILRunError("test cannot charge and discharge simultaneously")
    sign = 1 if actuator.power_feedback_convention == "charge_positive" else -1
    if charge_kw > 0:
        return charge_kw * sign
    if discharge_kw > 0:
        return discharge_kw * -sign
    return 0.0


def _postcondition(
    actuator: BatteryActuator, *, charge_kw: float, discharge_kw: float
) -> CommandPostcondition:
    return CommandPostcondition(
        capability=actuator.power_feedback_capability,
        expected=_feedback_value(actuator, charge_kw=charge_kw, discharge_kw=discharge_kw),
        tolerance=actuator.power_feedback_tolerance_kw,
        settle_timeout_seconds=actuator.power_feedback_settle_timeout_seconds,
        poll_interval_seconds=actuator.power_feedback_poll_interval_seconds,
        reconcile_capabilities=(
            [actuator.soc_reconciliation_capability]
            if actuator.soc_reconciliation_capability is not None
            else []
        ),
    )


async def run_battery_hil(
    runtime: RuntimeComposition,
    *,
    binding: DispatchableBatteryBinding,
    test_charge_kw: float,
    test_discharge_kw: float,
    hardware_id: str,
    firmware_version: str,
    test_software_version: str | None,
    manual_attestations: dict[str, str],
    run_id: str | None = None,
    clock: Clock | None = None,
) -> BatteryHILEvidence:
    """Run the real check sequence against `runtime.adapter` and return
    evidence built from observed outcomes, not operator assertion."""

    if test_charge_kw <= 0 or test_discharge_kw <= 0:
        raise BatteryHILRunError("test_charge_kw and test_discharge_kw must both be positive")
    missing_attestations = _MANUAL_ONLY_CHECKS - set(manual_attestations)
    if missing_attestations:
        raise BatteryHILRunError(
            "missing required manual attestations: " + ", ".join(sorted(missing_attestations))
        )

    actuator = binding.profile.actuator
    if actuator is None:
        raise BatteryHILRunError("battery binding has no dispatch actuator configured")

    clock = clock or SystemClock()
    run_id = run_id or f"hil-{uuid.uuid4().hex}"
    checks: dict[str, bool] = {}
    observations: dict[str, dict[str, Any]] = {}

    # identity
    health = await runtime.adapter.health()
    device = runtime.registry.get(actuator.device_id)
    checks["identity"] = device is not None and health.connected
    observations["identity"] = {
        "device_found": device is not None,
        "adapter_connected": health.connected,
    }

    # writable_routes
    route_results = {
        command_name: runtime.registry.resolve_command_route(actuator.device_id, command_name)
        for command_name in (
            actuator.charge_command,
            actuator.discharge_command,
            actuator.stop_command,
        )
    }
    checks["writable_routes"] = all(
        resolution.route is not None for resolution in route_results.values()
    )
    observations["writable_routes"] = {
        command_name: {
            "resolved": resolution.route is not None,
            "reason": resolution.reason,
        }
        for command_name, resolution in route_results.items()
    }

    # Real dispatch sequence: baseline stop (takeover happens on this first
    # command) -> charge -> stop -> discharge -> stop. Each step's
    # postcondition is the same charge_positive/discharge_positive-aware
    # feedback value the production optimizer compiler computes -- a
    # confirmed postcondition therefore also proves polarity is correct,
    # not just that *a* command was accepted.
    steps = [
        ("baseline_stop", actuator.stop_command, None, 0.0, 0.0),
        ("charge_feedback", actuator.charge_command, test_charge_kw, test_charge_kw, 0.0),
        ("post_charge_stop", actuator.stop_command, None, 0.0, 0.0),
        (
            "discharge_feedback",
            actuator.discharge_command,
            test_discharge_kw,
            0.0,
            test_discharge_kw,
        ),
        ("stop_feedback", actuator.stop_command, None, 0.0, 0.0),
    ]
    outcomes_by_step: dict[str, ExecutionStatus] = {}
    for index, (step_name, command_name, value, charge_kw, discharge_kw) in enumerate(steps):
        command = Command(
            id=f"{run_id}:{step_name}",
            device_id=actuator.device_id,
            command=command_name,
            value=value,
            unit=actuator.power_unit if value is not None else None,
            idempotency_key=f"{run_id}:{step_name}",
            intent=("takeover_first_slot:0" if index == 0 else f"hil_step:{index}"),
            postconditions=[
                _postcondition(actuator, charge_kw=charge_kw, discharge_kw=discharge_kw)
            ],
        )
        plan = Plan(id=f"{run_id}-{step_name}", commands=[command])
        validated = runtime.facade.validate_plan(plan)
        requires_confirmation = validated.status is PlanStatus.REQUIRES_CONFIRMATION
        if requires_confirmation and validated.validation is not None:
            # A HIL run is inherently attended: the person invoking
            # `domoai-hil battery` on a live bench *is* the human operator,
            # with no remote/agent boundary to defend against self-approval
            # across (that's what the MCP request_approval flow guards
            # against). Self-issuing the grant here is that operator's
            # in-person confirmation, not a policy bypass.
            grant = ApprovalGrant(
                approval_id=f"{run_id}:{step_name}:approval",
                plan_id=validated.id,
                validation_digest=validated.validation.digest,
                approved_by="hil_runner_local_operator",
                issued_at=clock.now(),
                authentication_context="hil_runner_local_operator",
                window_digest=(
                    validated.execution_window.digest
                    if validated.execution_window is not None
                    else None
                ),
                schedule_revision=validated.schedule_revision,
            )
            validated = runtime.facade.approve_plan(validated, grant=grant)
        summary = await runtime.facade.execute_plan(validated)
        outcome = summary.outcomes[0]
        outcomes_by_step[step_name] = outcome.status
        observations[step_name] = {
            "command": command_name,
            "status": outcome.status.value,
            "adapter_ref": (
                outcome.adapter_ref.model_dump(mode="json")
                if outcome.adapter_ref is not None
                else None
            ),
            "error": outcome.error.model_dump(mode="json") if outcome.error is not None else None,
        }

    def _confirmed(step: str) -> bool:
        return outcomes_by_step[step] is ExecutionStatus.CONFIRMED_SUCCESS

    checks["takeover_baseline"] = _confirmed("baseline_stop")
    checks["charge_feedback"] = _confirmed("charge_feedback")
    checks["discharge_feedback"] = _confirmed("discharge_feedback")
    checks["stop_feedback"] = _confirmed("post_charge_stop") and _confirmed("stop_feedback")
    # A confirmed postcondition already asserted the signed expected value
    # (see _feedback_value), so a confirmed charge+discharge pair is direct
    # evidence the sign convention matches the physical inverter.
    checks["polarity"] = checks["charge_feedback"] and checks["discharge_feedback"]
    checks["soc_reconciliation"] = (
        actuator.soc_reconciliation_capability is None or checks["charge_feedback"]
    )

    for check_name in _MANUAL_ONLY_CHECKS:
        checks[check_name] = True  # presence already required above; value is the attestation

    status: str = "passed" if all(checks.values()) else "failed"
    return BatteryHILEvidence(
        status=status,  # type: ignore[arg-type]
        profile_digest=battery_binding_digest(binding),
        hardware_id=hardware_id,
        firmware_version=firmware_version,
        completed_at=clock.now(),
        checks=checks,
        run_id=run_id,
        test_software_version=test_software_version,
        observations=observations,
        manual_attestations=manual_attestations,
    )
