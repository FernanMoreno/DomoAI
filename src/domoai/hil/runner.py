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

import hashlib
import json
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from domoai.config.battery_qualification import (
    REQUIRED_HIL_CHECKS,
    BatteryHILEvidence,
    battery_binding_digest,
    battery_identity_digest,
)
from domoai.domain.models import (
    AdapterIdentityObservation,
    Command,
    CommandPostcondition,
    ControlLeaseStatus,
    ExecutionStatus,
    Plan,
    PlanStatus,
    TakeoverResult,
)
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
_NOT_VERIFIED_MARKERS = ("not verified", "not tested", "not run")
_NOT_EXERCISED_MARKERS = ("not exercised", "not exercised by")


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
    configured_binding = getattr(runtime, "dispatchable_battery_binding", None)
    if configured_binding is None:
        raise BatteryHILRunError(
            "HIL requires a runtime built with the exact dispatchable battery binding"
        )
    if configured_binding != binding:
        raise BatteryHILRunError("HIL binding does not match the runtime binding")
    if test_charge_kw > binding.profile.max_charge_kw:
        raise BatteryHILRunError("test_charge_kw exceeds the battery profile envelope")
    if test_discharge_kw > binding.profile.max_discharge_kw:
        raise BatteryHILRunError("test_discharge_kw exceeds the battery profile envelope")
    ceiling = getattr(runtime.settings, "battery_hil_power_ceiling_kw", None)
    if ceiling is not None and max(test_charge_kw, test_discharge_kw) > ceiling:
        raise BatteryHILRunError("HIL test power exceeds the deployment safety ceiling")

    clock = clock or SystemClock()
    run_id = run_id or f"hil-{uuid.uuid4().hex}"
    checks: dict[str, bool] = {}
    observations: dict[str, dict[str, Any]] = {}

    # identity
    health = await runtime.adapter.health()
    device = runtime.registry.get(actuator.device_id)
    checks["identity"] = device is not None and health.connected
    identity_observation: AdapterIdentityObservation | None = None
    identity_reader = getattr(runtime.adapter, "read_hil_identity", None)
    if callable(identity_reader):
        try:
            candidate = await identity_reader()
        except Exception:
            candidate = None
        if isinstance(candidate, AdapterIdentityObservation):
            identity_observation = candidate
    hardware_identity_observed = bool(
        identity_observation is not None
        and identity_observation.hardware_id == hardware_id
        and identity_observation.source_ref.adapter_id == binding.provider_id
        and identity_observation.observed_at <= clock.now()
    )
    firmware_identity_observed = bool(
        hardware_identity_observed
        and identity_observation is not None
        and identity_observation.firmware_version == firmware_version
    )
    observations["identity"] = {
        "device_found": device is not None,
        "adapter_connected": health.connected,
        "hardware_identity_observed": hardware_identity_observed,
        "firmware_identity_observed": firmware_identity_observed,
        "identity_observed_at": (
            identity_observation.observed_at.isoformat()
            if identity_observation is not None
            else None
        ),
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
    command_attempted = False

    async def _execute_step(
        index: int,
        step_name: str,
        command_name: str,
        value: float | None,
        charge_kw: float,
        discharge_kw: float,
    ) -> None:
        nonlocal command_attempted
        command_attempted = command_attempted or charge_kw > 0 or discharge_kw > 0
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
            # `domoai-hil battery` on a live bench *is* the human operator.
            # The grant is still issued and consumed by the server-owned store
            # so the physical admission boundary cannot accept a fabricated
            # Approval projection.
            grant = runtime.approval_store.issue_attended_local(
                validated,
                operator_id="hil_runner_local_operator",
                session_id=f"hil:{run_id}",
            )
            grant = runtime.approval_store.consume(grant.approval_id, validated)
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

    sequence_error: Exception | None = None
    cleanup_error: Exception | None = None
    try:
        for index, (step_name, command_name, value, charge_kw, discharge_kw) in enumerate(steps):
            await _execute_step(
                index, step_name, command_name, value, charge_kw, discharge_kw
            )
    except Exception as error:
        sequence_error = error
    finally:
        # A latched command must be stopped even when validation, transport,
        # readback, or an unexpected runner error interrupts the sequence.
        if (
            command_attempted
            and outcomes_by_step.get("stop_feedback") is not ExecutionStatus.CONFIRMED_SUCCESS
        ):
            try:
                await _execute_step(
                    len(steps),
                    "emergency_stop",
                    actuator.stop_command,
                    None,
                    0.0,
                    0.0,
                )
                if outcomes_by_step.get("emergency_stop") is not ExecutionStatus.CONFIRMED_SUCCESS:
                    raise BatteryHILRunError("emergency stop did not confirm zero power")
            except Exception as error:
                cleanup_error = error
    if sequence_error is not None:
        if cleanup_error is not None:
            raise BatteryHILRunError(
                f"HIL sequence failed and emergency stop was not confirmed: {cleanup_error}"
            ) from sequence_error
        raise BatteryHILRunError(f"HIL sequence failed: {sequence_error}") from sequence_error
    if cleanup_error is not None:
        raise BatteryHILRunError(
            f"HIL emergency stop was not confirmed: {cleanup_error}"
        ) from cleanup_error

    def _confirmed(step: str) -> bool:
        return outcomes_by_step[step] is ExecutionStatus.CONFIRMED_SUCCESS

    takeover_payload = next(
        (
            event.payload
            for event in reversed(runtime.audit.events)
            if event.event_type == "control_takeover_result"
            and event.payload.get("plan_id") == f"{run_id}-baseline_stop"
        ),
        None,
    )
    takeover_result: TakeoverResult | None = None
    if takeover_payload is not None:
        try:
            takeover_result = TakeoverResult.model_validate(takeover_payload)
        except (TypeError, ValueError):
            takeover_result = None
    first_command_result = next(
        (
            event.payload
            for event in reversed(runtime.audit.events)
            if event.event_type == "control_takeover_first_command_result"
            and event.payload.get("plan_id") == f"{run_id}-baseline_stop"
        ),
        None,
    )
    # The adapter's acquisition response is emitted before the executor sends
    # the first command, so ``TakeoverResult.first_command_confirmed`` may
    # legitimately still be false.  The authoritative confirmation is the
    # executor outcome tied to the exact takeover command and plan.
    first_command_confirmed = bool(
        takeover_result is not None
        and isinstance(first_command_result, dict)
        and first_command_result.get("plan_id") == takeover_result.plan_id
        and first_command_result.get("command_id") == takeover_result.first_command_id
        and first_command_result.get("confirmed") is True
    )
    baseline = takeover_result.baseline if takeover_result is not None else None
    lease_is_live = bool(
        takeover_result is not None
        and takeover_result.acquired_at <= clock.now() < takeover_result.expires_at
    )
    baseline_is_observed = bool(
        baseline is not None
        and baseline.device_id == binding.device_id
        and baseline.capability == actuator.power_feedback_capability
        and baseline.source_ref.adapter_id == binding.provider_id
        and baseline.power_kw is not None
        and baseline.observed_at <= baseline.received_at <= clock.now()
    )
    checks["takeover_baseline"] = bool(
        configured_binding == binding
        and takeover_result is not None
        and takeover_result.status is ControlLeaseStatus.ACQUIRED
        and takeover_result.owner == binding.control_policy.owner
        and takeover_result.device_id == binding.device_id
        and takeover_result.plan_id == f"{run_id}-baseline_stop"
        and first_command_confirmed
        and lease_is_live
        and baseline_is_observed
        and _confirmed("baseline_stop")
    )
    if takeover_payload is not None:
        observations["takeover_baseline"] = takeover_payload
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
        note = manual_attestations[check_name].lower()
        if any(marker in note for marker in _NOT_EXERCISED_MARKERS):
            manual_status = "not_exercised"
        elif "not applicable" in note:
            manual_status = "not_applicable"
        elif any(marker in note for marker in _NOT_VERIFIED_MARKERS):
            manual_status = "not_verified"
        else:
            manual_status = "verified"
        checks[check_name] = manual_status == "verified"
        observations.setdefault("manual_checks", {})[check_name] = {
            "status": manual_status,
            "note": manual_attestations[check_name],
        }

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
        provider_id=binding.provider_id,
        runtime_binding_digest=battery_binding_digest(binding),
        takeover_evidence_digest=(
            "sha256:"
            + hashlib.sha256(
                json.dumps(takeover_payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if takeover_payload is not None
            else None
        ),
        qualification_expires_at=clock.now() + timedelta(hours=24),
        # CLI labels count only when the adapter itself returned the same
        # values with a provider observation timestamp.
        hardware_identity_observed=hardware_identity_observed,
        firmware_identity_observed=firmware_identity_observed,
        identity_observed_at=(
            identity_observation.observed_at if identity_observation is not None else None
        ),
        identity_evidence_digest=(
            battery_identity_digest(
                hardware_id=hardware_id,
                firmware_version=firmware_version,
                provider_id=binding.provider_id,
                profile_digest=battery_binding_digest(binding),
                observed_at=identity_observation.observed_at,
            )
            if firmware_identity_observed and identity_observation is not None
            else None
        ),
        manual_check_status={
            check: observations["manual_checks"][check]["status"]
            for check in _MANUAL_ONLY_CHECKS
        },
    )
