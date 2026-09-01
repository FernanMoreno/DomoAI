"""Plan execution boundary with idempotency and audit outcomes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from domoai.application.dynamic_safety import DynamicSafetyGuard
from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.plan_service import PlanService
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    Capability,
    Command,
    ErrorDetail,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    PolicyDecision,
    Precondition,
    StateSnapshot,
    StateStatus,
)
from domoai.runtime.clock import Clock
from domoai.runtime.control_takeover import ControlTakeoverPort
from domoai.runtime.events import AuditLog
from domoai.runtime.execution_context import ExecutionContext, current_execution_principal
from domoai.runtime.freshness import FreshnessDecision, FreshnessEvaluator
from domoai.runtime.ports import (
    AdapterPort,
    ExecutionOutcomePort,
    PlanRecordPort,
    StateSnapshotSinkPort,
)
from domoai.runtime.safety_kernel import SafetyKernel


@dataclass(frozen=True)
class _PreflightResult:
    command: Command
    before_state: StateSnapshot | None
    error: ErrorDetail | None


@dataclass(frozen=True)
class _PreconditionFailure:
    precondition: Precondition
    decision: FreshnessDecision

    def as_payload(self) -> dict[str, object]:
        return {
            **self.precondition.model_dump(mode="json"),
            "freshness": self.decision.details(),
        }


class _ReadbackPersistenceError(Exception):
    """Internal marker for a readback that could not become durable."""


class _ControlLeaseExpired(Exception):
    """Internal marker for a lost latched-actuator lease."""


class PlanExecutor:
    def __init__(
        self,
        adapter: AdapterPort,
        plan_service: PlanService,
        audit: AuditLog,
        *,
        plan_repository: PlanRecordPort | None = None,
        outcome_repository: ExecutionOutcomePort | None = None,
        state_snapshot_repository: StateSnapshotSinkPort | None = None,
        clock: Clock | None = None,
        safety_kernel: SafetyKernel | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        control_takeover: ControlTakeoverPort | None = None,
        execution_admission: ExecutionAdmission | None = None,
        dynamic_safety_guard: DynamicSafetyGuard | None = None,
    ) -> None:
        self.adapter = adapter
        self.plan_service = plan_service
        self.audit = audit
        self.plan_repository = plan_repository
        self.outcome_repository = outcome_repository
        self.state_snapshot_repository = state_snapshot_repository
        self.clock = clock or plan_service.state_store.clock
        self.freshness_evaluator = FreshnessEvaluator(
            self.clock, max_age=plan_service.state_store.stale_after
        )
        self.safety_kernel = safety_kernel
        self._sleep = sleep or asyncio.sleep
        self.control_takeover = control_takeover
        self.execution_admission = execution_admission
        self.dynamic_safety_guard = dynamic_safety_guard

    _NON_CLAIMABLE_STATUSES = {
        PlanStatus.EXECUTING,
        PlanStatus.COMPLETED,
        PlanStatus.PARTIALLY_FAILED,
        PlanStatus.FAILED,
        PlanStatus.UNKNOWN,
        PlanStatus.CANCELLED,
    }

    _CLAIMABLE_STATUSES = frozenset({PlanStatus.READY, PlanStatus.APPROVED})

    async def execute(
        self,
        plan: Plan,
        *,
        state_version_overrides: dict[str, int] | None = None,
        aggregate_owner: bool = False,
    ) -> ExecutionSummary:
        if self.execution_admission is not None:
            await self.execution_admission.admit(plan, aggregate_owner=aggregate_owner)
        if plan.execute_at is not None and plan.execute_at > self.clock.now():
            raise DomainError(
                ErrorCode.NOT_YET_DUE,
                "Plan is not yet due; wait until its scheduled execution time",
            )
        if self.plan_repository is not None:
            persisted = await self.plan_repository.get(plan.id)
            if persisted is not None and persisted.status in self._NON_CLAIMABLE_STATUSES:
                raise DomainError(
                    ErrorCode.INVALID_TRANSITION,
                    "Plan is already executing or has reached a terminal status",
                )
        self.plan_service.assert_executable(
            plan, state_version_overrides=state_version_overrides
        )
        if self.plan_repository is not None:
            claimed = await self.plan_repository.claim_for_execution(
                plan.model_copy(update={"status": PlanStatus.EXECUTING}),
                allowed_statuses=self._CLAIMABLE_STATUSES,
            )
            if not claimed:
                raise DomainError(
                    ErrorCode.INVALID_TRANSITION,
                    "Plan is already executing or has reached a terminal status",
                )
        execution_attempt_id = str(uuid4())
        self.audit.append(
            event_type="plan_execution_started",
            actor="runtime",
            subject_id=plan.id,
            payload={
                "plan_id": plan.id,
                "agent_request_id": plan.agent_request_id,
                "client_principal_id": current_execution_principal(),
                "execution_attempt_id": execution_attempt_id,
            },
        )
        preflight = await self._preflight(plan)
        if preflight is not None:
            preflight_outcomes = self._preflight_outcomes(plan, execution_attempt_id, preflight)
            for outcome in preflight_outcomes:
                if self.outcome_repository is not None:
                    await self.outcome_repository.save(outcome)
                self.audit.append(
                    event_type="command_execution_outcome",
                    actor="runtime",
                    subject_id=outcome.command_id,
                    payload={
                        "plan_id": plan.id,
                        "status": outcome.status.value,
                        "client_principal_id": current_execution_principal(),
                        "execution_attempt_id": execution_attempt_id,
                        "adapter_request_id": outcome.adapter_request_id,
                    },
                )
            summary = ExecutionSummary(outcomes=preflight_outcomes)
            if self.plan_repository is not None:
                await self.plan_repository.settle_execution(
                    plan.model_copy(
                        update={
                            "status": self._terminal_plan_status(preflight_outcomes),
                            "execution": summary,
                        }
                    )
                )
            failed_commands = [
                result.command.id for result in preflight if result.error is not None
            ]
            self.audit.append(
                event_type="plan_preflight_rejected",
                actor="runtime",
                subject_id=plan.id,
                payload={
                    "plan_id": plan.id,
                    "execution_attempt_id": execution_attempt_id,
                    "failed_command_ids": failed_commands,
                    "failure_count": len(failed_commands),
                },
            )
            self.audit.append(
                event_type="plan_execution_completed",
                actor="runtime",
                subject_id=plan.id,
                payload={
                    "plan_id": plan.id,
                    "status": self._terminal_plan_status(preflight_outcomes).value,
                    "outcome_count": len(preflight_outcomes),
                    "execution_attempt_id": execution_attempt_id,
                },
            )
            return summary
        takeover_first_command_id: str | None = None
        if self.control_takeover is not None:
            takeover = await self.control_takeover.acquire_for_plan(
                plan_id=plan.id, commands=plan.commands
            )
            if takeover is not None:
                self.audit.append(
                    event_type="control_takeover_result",
                    actor="runtime",
                    subject_id=plan.id,
                    payload=takeover.model_dump(mode="json"),
                )
                if takeover.status.value != "acquired":
                    takeover_outcomes = [
                        ExecutionOutcome(
                            plan_id=plan.id,
                            command_id=command.id,
                            execution_attempt_id=execution_attempt_id,
                            status=ExecutionStatus.REJECTED,
                            completed_at=self.clock.now(),
                            error=ErrorDetail(
                                code=ErrorCode.CONTROL_TAKEOVER_FAILED,
                                message="Physical control was not acquired before dispatch",
                                device_id=command.device_id,
                                retryable=True,
                                details={
                                    "failure_code": takeover.failure_code,
                                    "takeover_evidence_digest": takeover.evidence_digest,
                                },
                            ),
                        )
                        for command in plan.commands
                    ]
                    for outcome in takeover_outcomes:
                        if self.outcome_repository is not None:
                            await self.outcome_repository.save(outcome)
                        self.audit.append(
                            event_type="command_execution_outcome",
                            actor="runtime",
                            subject_id=outcome.command_id,
                            payload={
                                "plan_id": plan.id,
                                "status": outcome.status.value,
                                "client_principal_id": current_execution_principal(),
                                "control_takeover_failed": True,
                            },
                        )
                    summary = ExecutionSummary(outcomes=takeover_outcomes)
                    if self.plan_repository is not None:
                        await self.plan_repository.settle_execution(
                            plan.model_copy(
                                update={
                                    "status": PlanStatus.FAILED,
                                    "execution": summary,
                                }
                            )
                        )
                    return summary
                takeover_first_command_id = takeover.first_command_id
        outcomes: list[ExecutionOutcome] = []
        seen_keys: set[str] = set()
        for command_index, command in enumerate(plan.commands):
            semantic = self.plan_service.validate_command_semantics(command)
            command = semantic.command
            capability = semantic.capability
            if semantic.errors:
                outcome = ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=command.id,
                    execution_attempt_id=execution_attempt_id,
                    status=ExecutionStatus.REJECTED,
                    completed_at=self.clock.now(),
                    error=semantic.errors[0].model_copy(
                        update={
                            "details": {
                                **semantic.errors[0].details,
                                "phase": "jit",
                            }
                        }
                    ),
                )
                outcomes.append(outcome)
                if self.outcome_repository is not None:
                    await self.outcome_repository.save(outcome)
                self.audit.append(
                    event_type="command_execution_outcome",
                    actor="runtime",
                    subject_id=command.id,
                    payload={
                        "plan_id": plan.id,
                        "status": outcome.status.value,
                        "client_principal_id": current_execution_principal(),
                        "execution_attempt_id": execution_attempt_id,
                    },
                )
                continue
            if command.idempotency_key in seen_keys:
                raise DomainError(
                    ErrorCode.DUPLICATE_COMMAND,
                    "Duplicate command idempotency key detected during execution",
                )
            seen_keys.add(command.idempotency_key)
            before_state = (
                await self.plan_service.state_store.get(command.device_id, capability.name)
                if capability is not None
                else None
            )
            failed_preconditions = await self._failed_preconditions(
                command,
                policy_decision=(
                    plan.policy_decisions[command_index]
                    if command_index < len(plan.policy_decisions)
                    else None
                ),
            )
            if failed_preconditions:
                outcome = ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=command.id,
                    execution_attempt_id=execution_attempt_id,
                    status=ExecutionStatus.REJECTED,
                    before_state=before_state,
                    completed_at=self.clock.now(),
                    error=ErrorDetail(
                        code=ErrorCode.PRECONDITION_FAILED,
                        message=f"{len(failed_preconditions)} precondition(s) not satisfied",
                        retryable=True,
                        details={
                            "preconditions": [
                                item.as_payload() for item in failed_preconditions
                            ]
                        },
                    ),
                )
            elif (safety_error := self._safety_violation(command, capability)) is not None:
                outcome = ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=command.id,
                    execution_attempt_id=execution_attempt_id,
                    status=ExecutionStatus.REJECTED,
                    before_state=before_state,
                    completed_at=self.clock.now(),
                    error=safety_error,
                )
            elif (
                self.dynamic_safety_guard is not None
                and (dynamic_safety_error := await self.dynamic_safety_guard.check(command))
                is not None
            ):
                outcome = ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=command.id,
                    execution_attempt_id=execution_attempt_id,
                    status=ExecutionStatus.REJECTED,
                    before_state=before_state,
                    completed_at=self.clock.now(),
                    error=dynamic_safety_error,
                )
            else:
                adapter_request_id = str(uuid4())
                execution_context = ExecutionContext(
                    agent_request_id=plan.agent_request_id,
                    plan_id=plan.id,
                    execution_attempt_id=execution_attempt_id,
                    adapter_request_id=adapter_request_id,
                    client_principal_id=current_execution_principal(),
                )
                try:
                    assert_ownership = getattr(self.control_takeover, "assert_still_owned", None)
                    if (
                        takeover_first_command_id is not None
                        and callable(assert_ownership)
                        and not await assert_ownership(plan_id=plan.id)
                    ):
                        raise _ControlLeaseExpired
                    acknowledgement = await self.adapter.execute(command, execution_context)
                    after_state = None
                    status = ExecutionStatus.REJECTED
                    error: ErrorDetail | None = None
                    if acknowledgement.accepted:
                        after_state, readback_error = await self._readback_until_settled(
                            command,
                            capability.name if capability else None,
                            before_state=before_state,
                        )
                        if readback_error is not None:
                            status = ExecutionStatus.UNKNOWN
                            if isinstance(readback_error, _ReadbackPersistenceError):
                                self.audit.append(
                                    event_type="post_write_reconciliation_failed",
                                    actor="runtime",
                                    subject_id=command.id,
                                    payload={
                                        "command_id": command.id,
                                        "device_id": command.device_id,
                                        "capability": (
                                            command.postconditions[0].capability
                                            if command.postconditions
                                            else capability.name if capability else None
                                        ),
                                        "error_code": ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                                        "reason": "persistence_failed",
                                    },
                                )
                                error = ErrorDetail(
                                    code=ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                                    message="Physical readback could not be persisted",
                                    capability=(
                                        command.postconditions[0].capability
                                        if command.postconditions
                                        else capability.name if capability else None
                                    ),
                                    retryable=True,
                                    details={"reason": "persistence_failed"},
                                )
                            else:
                                error = ErrorDetail(
                                    code=ErrorCode.ADAPTER_UNAVAILABLE,
                                    message=str(readback_error),
                                    retryable=True,
                                )
                        elif self._postcondition_matches(
                            command,
                            capability.name if capability else None,
                            after_state,
                            before_state=before_state,
                        ):
                            reconciliation_error = await self._reconcile_post_write(command)
                            if reconciliation_error is not None:
                                status = ExecutionStatus.UNKNOWN
                                error = reconciliation_error
                            else:
                                status = ExecutionStatus.CONFIRMED_SUCCESS
                        else:
                            status = ExecutionStatus.UNKNOWN
                            error = ErrorDetail(
                                code=ErrorCode.EXECUTION_FAILED,
                                message=(
                                    "Adapter accepted the command but postcondition "
                                    "was not confirmed"
                                ),
                                retryable=True,
                            )
                    else:
                        error = ErrorDetail(
                            code=ErrorCode.EXECUTION_FAILED,
                            message=acknowledgement.message or "Adapter rejected command",
                            retryable=False,
                        )
                    outcome = ExecutionOutcome(
                        plan_id=plan.id,
                        command_id=command.id,
                        execution_attempt_id=execution_attempt_id,
                        adapter_request_id=adapter_request_id,
                        status=status,
                        adapter_ref=acknowledgement.source_ref,
                        before_state=before_state,
                        after_state=after_state,
                        completed_at=self.clock.now(),
                        error=error,
                    )
                except _ControlLeaseExpired:
                    emergency_stop = getattr(self.control_takeover, "emergency_stop", None)
                    stop_confirmed = False
                    if callable(emergency_stop):
                        stop_confirmed = await emergency_stop(
                            plan_id=plan.id,
                            execution_attempt_id=execution_attempt_id,
                        )
                        self.audit.append(
                            event_type="control_supervisor_emergency_stop",
                            actor="runtime",
                            subject_id=plan.id,
                            payload={"confirmed": stop_confirmed, "reason": "lease_expired"},
                        )
                    outcome = ExecutionOutcome(
                        plan_id=plan.id,
                        command_id=command.id,
                        execution_attempt_id=execution_attempt_id,
                        adapter_request_id=adapter_request_id,
                        status=ExecutionStatus.REJECTED,
                        before_state=before_state,
                        completed_at=self.clock.now(),
                        error=ErrorDetail(
                            code=ErrorCode.CONTROL_TAKEOVER_FAILED,
                            message="Physical control lease expired before dispatch",
                            retryable=True,
                            details={"emergency_stop_confirmed": stop_confirmed},
                        ),
                    )
                except (ConnectionError, OSError, TimeoutError) as error:
                    outcome = ExecutionOutcome(
                        plan_id=plan.id,
                        command_id=command.id,
                        execution_attempt_id=execution_attempt_id,
                        adapter_request_id=adapter_request_id,
                        status=ExecutionStatus.UNAVAILABLE,
                        before_state=before_state,
                        completed_at=self.clock.now(),
                        error=ErrorDetail(
                            code=ErrorCode.ADAPTER_UNAVAILABLE,
                            message=str(error),
                            retryable=True,
                        ),
                    )
            outcomes.append(outcome)
            if self.outcome_repository is not None:
                await self.outcome_repository.save(outcome)
            self.audit.append(
                event_type="command_execution_outcome",
                actor="runtime",
                subject_id=command.id,
                payload={
                    "plan_id": plan.id,
                    "status": outcome.status.value,
                    "client_principal_id": current_execution_principal(),
                    "execution_attempt_id": execution_attempt_id,
                    "adapter_request_id": outcome.adapter_request_id,
                },
            )
            if takeover_first_command_id == outcome.command_id:
                self.audit.append(
                    event_type="control_takeover_first_command_result",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={
                        "plan_id": plan.id,
                        "command_id": outcome.command_id,
                        "confirmed": outcome.status is ExecutionStatus.CONFIRMED_SUCCESS,
                    },
                )
                if outcome.status is not ExecutionStatus.CONFIRMED_SUCCESS:
                    for remaining in plan.commands[command_index + 1 :]:
                        blocked = ExecutionOutcome(
                            plan_id=plan.id,
                            command_id=remaining.id,
                            execution_attempt_id=execution_attempt_id,
                            status=ExecutionStatus.REJECTED,
                            completed_at=self.clock.now(),
                            error=ErrorDetail(
                                code=ErrorCode.CONTROL_TAKEOVER_FAILED,
                                message=(
                                    "Later dispatch command blocked because the first "
                                    "takeover command was not confirmed"
                                ),
                                device_id=remaining.device_id,
                                retryable=True,
                                details={"first_command_id": outcome.command_id},
                            ),
                        )
                        outcomes.append(blocked)
                        if self.outcome_repository is not None:
                            await self.outcome_repository.save(blocked)
                    break
        if takeover_first_command_id is not None:
            release_for_plan = getattr(self.control_takeover, "release_for_plan", None)
            if callable(release_for_plan):
                try:
                    release_confirmed = await release_for_plan(
                        plan_id=plan.id,
                        execution_attempt_id=f"{execution_attempt_id}:release",
                    )
                except Exception as error:
                    release_confirmed = False
                    self.audit.append(
                        event_type="control_lease_release_failed",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={"error": str(error)[:200]},
                    )
                self.audit.append(
                    event_type="control_lease_released",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={"confirmed": release_confirmed},
                )
        summary = ExecutionSummary(outcomes=outcomes)
        if self.plan_repository is not None:
            terminal_status = self._terminal_plan_status(outcomes)
            await self.plan_repository.settle_execution(
                plan.model_copy(update={"status": terminal_status, "execution": summary})
            )
        self.audit.append(
            event_type="plan_execution_completed",
            actor="runtime",
            subject_id=plan.id,
            payload={
                "plan_id": plan.id,
                "status": self._terminal_plan_status(outcomes).value,
                "client_principal_id": current_execution_principal(),
                "outcome_count": len(outcomes),
                "execution_attempt_id": execution_attempt_id,
            },
        )
        return summary

    def _safety_violation(
        self, command: Command, capability: Capability | None
    ) -> ErrorDetail | None:
        if self.safety_kernel is None or capability is None:
            return None
        device = self.plan_service.registry.get(command.device_id)
        if device is None:
            return None
        error = self.safety_kernel.check(
            device_type=device.type, capability=capability.name, value=command.value
        )
        return (
            error.model_copy(update={"device_id": command.device_id})
            if error is not None
            else None
        )

    async def _preflight(self, plan: Plan) -> list[_PreflightResult] | None:
        projected_state: dict[tuple[str, str], StateSnapshot] = {}
        results: list[_PreflightResult] = []
        for command_index, command in enumerate(plan.commands):
            semantic = self.plan_service.validate_command_semantics(command)
            command = semantic.command
            capability = semantic.capability
            before_state = (
                await self.plan_service.state_store.get(command.device_id, capability.name)
                if capability is not None
                else None
            )
            failed_preconditions = await self._failed_preconditions(
                command,
                projected_state=projected_state,
                policy_decision=(
                    plan.policy_decisions[command_index]
                    if command_index < len(plan.policy_decisions)
                    else None
                ),
            )
            error: ErrorDetail | None = None
            if semantic.errors:
                error = semantic.errors[0].model_copy(
                    update={
                        "details": {
                            **semantic.errors[0].details,
                            "phase": "plan_preflight",
                        }
                    }
                )
            elif failed_preconditions:
                error = ErrorDetail(
                    code=ErrorCode.PRECONDITION_FAILED,
                    message=f"{len(failed_preconditions)} precondition(s) not satisfied",
                    device_id=command.device_id,
                    capability=capability.name if capability is not None else None,
                    retryable=True,
                    details={
                        "phase": "plan_preflight",
                        "preconditions": [
                            item.as_payload() for item in failed_preconditions
                        ],
                    },
                )
            else:
                safety_error = self._safety_violation(command, capability)
                error = (
                    safety_error.model_copy(
                        update={
                            "details": {
                                **safety_error.details,
                                "phase": "plan_preflight",
                            }
                        }
                    )
                    if safety_error is not None
                    else None
                )
            results.append(_PreflightResult(command, before_state, error))
            projected = self._projected_postcondition(command, capability)
            if projected is not None and before_state is not None:
                capability_name, value = projected
                projected_state[(command.device_id, capability_name)] = before_state.model_copy(
                    update={"capability": capability_name, "value": value}
                )
        return results if any(result.error is not None for result in results) else None

    def _preflight_outcomes(
        self, plan: Plan, execution_attempt_id: str, results: list[_PreflightResult]
    ) -> list[ExecutionOutcome]:
        failed_command_ids = [result.command.id for result in results if result.error is not None]
        outcomes: list[ExecutionOutcome] = []
        for result in results:
            error = result.error or ErrorDetail(
                code=ErrorCode.VALIDATION_ERROR,
                message="Plan preflight rejected execution before any physical write",
                retryable=False,
                details={
                    "phase": "plan_preflight",
                    "blocked_by": failed_command_ids,
                },
            )
            outcomes.append(
                ExecutionOutcome(
                    plan_id=plan.id,
                    command_id=result.command.id,
                    execution_attempt_id=execution_attempt_id,
                    status=ExecutionStatus.REJECTED,
                    before_state=result.before_state,
                    completed_at=self.clock.now(),
                    error=error,
                )
            )
        return outcomes

    async def _failed_preconditions(
        self,
        command: Command,
        *,
        projected_state: dict[tuple[str, str], StateSnapshot] | None = None,
        policy_decision: PolicyDecision | None = None,
    ) -> list[_PreconditionFailure]:
        failed: list[_PreconditionFailure] = []
        for precondition in command.preconditions:
            key = (precondition.device_id, precondition.capability)
            snapshot = (
                projected_state[key]
                if projected_state is not None and key in projected_state
                else await self.plan_service.state_store.get(
                    precondition.device_id, precondition.capability
                )
            )
            decision = self.freshness_evaluator.evaluate(
                snapshot,
                precondition,
                policy_decision,
                source_revision=self.plan_service.state_store.state_version(
                    precondition.device_id, precondition.capability
                ),
            )
            if decision.stale_exception:
                self.audit.append(
                    event_type="precondition_stale_exception",
                    actor="policy-engine",
                    subject_id=command.id,
                    payload={
                        "device_id": precondition.device_id,
                        "capability": precondition.capability,
                        "policy_id": policy_decision.policy_id if policy_decision else None,
                        "authority": "policy-engine",
                        "justification": policy_decision.reason if policy_decision else None,
                        "evidence": decision.details(),
                    },
                )
            if not decision.satisfied:
                failed.append(_PreconditionFailure(precondition, decision))
        return failed

    @staticmethod
    def _projected_postcondition(
        command: Command, capability: Capability | None
    ) -> tuple[str, object] | None:
        if command.postconditions:
            postcondition = command.postconditions[0]
            if postcondition.verification != "equals":
                return None
            return postcondition.capability, postcondition.expected
        if capability is None:
            return None
        if command.command == "turn_on":
            return capability.name, True
        if command.command == "turn_off":
            return capability.name, False
        if command.command == "open":
            return capability.name, 100
        if command.command == "close":
            return capability.name, 0
        if command.command.startswith("set_") and command.value is not None:
            return capability.name, command.value
        return None

    async def _readback(
        self, command: Command, capability_name: str | None
    ) -> StateSnapshot | None:
        feedback_capability = (
            command.postconditions[0].capability
            if command.postconditions
            else capability_name
        )
        if feedback_capability is None:
            return None
        device = self.plan_service.registry.get(command.device_id)
        if device is None:
            return None
        if command.postconditions:
            routes = [
                route
                for route in self.plan_service.registry.routes_for(
                    command.device_id, feedback_capability
                )
                if route.available and route.readable
            ]
            route = routes[0] if len(routes) == 1 else None
        else:
            route = self.plan_service.registry.resolve_command_route(
                command.device_id, command.command
            ).route
        if route is None:
            return None
        snapshots = await self.adapter.read_state([route.source_ref])
        snapshot = next(
            (
                item
                for item in snapshots
                if item.capability == feedback_capability
                and item.source_ref.adapter_id == route.source_ref.adapter_id
                and item.source_ref.external_id == route.source_ref.external_id
            ),
            None,
        )
        if snapshot is None:
            return None
        normalized = snapshot.model_copy(update={"device_id": command.device_id})
        await self._persist_snapshot(normalized)
        return normalized

    async def _persist_snapshot(self, snapshot: StateSnapshot) -> None:
        try:
            await self.plan_service.state_store.save(snapshot)
            # RuntimeComposition binds StateStore to the single durable
            # RuntimeStatePersistencePort. Keep the legacy sink only for
            # standalone executor fixtures that intentionally do not bind a
            # StateStore; production therefore never performs two writes.
            if (
                not self.plan_service.state_store.persistence_bound
                and self.state_snapshot_repository is not None
            ):
                await self.state_snapshot_repository.save(snapshot)
        except Exception as error:
            raise _ReadbackPersistenceError(
                "state snapshot persistence failed"
            ) from error

    async def _reconcile_post_write(self, command: Command) -> ErrorDetail | None:
        postcondition = command.postconditions[0] if command.postconditions else None
        if postcondition is None or not postcondition.reconcile_capabilities:
            return None
        for capability_name in postcondition.reconcile_capabilities:
            routes = [
                route
                for route in self.plan_service.registry.routes_for(
                    command.device_id, capability_name
                )
                if route.available and route.readable
            ]
            if len(routes) != 1:
                error = ErrorDetail(
                    code=ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                    message="Post-write reconciliation route is not uniquely available",
                    device_id=command.device_id,
                    capability=capability_name,
                    retryable=True,
                    details={"reason": "route_unavailable_or_ambiguous"},
                )
            else:
                try:
                    snapshots = await self.adapter.read_state([routes[0].source_ref])
                    snapshot = next(
                        (
                            item
                            for item in snapshots
                            if item.capability == capability_name
                            and item.source_ref.adapter_id == routes[0].source_ref.adapter_id
                            and item.source_ref.external_id
                            == routes[0].source_ref.external_id
                        ),
                        None,
                    )
                    if snapshot is None:
                        error = ErrorDetail(
                            code=ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                            message="Post-write reconciliation returned no observation",
                            device_id=command.device_id,
                            capability=capability_name,
                            retryable=True,
                            details={"reason": "observation_missing"},
                        )
                    else:
                        normalized = snapshot.model_copy(update={"device_id": command.device_id})
                        await self._persist_snapshot(normalized)
                        if normalized.status is not StateStatus.CURRENT:
                            error = ErrorDetail(
                                code=ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                                message="Post-write reconciliation observation is not current",
                                device_id=command.device_id,
                                capability=capability_name,
                                retryable=True,
                                details={"reason": normalized.status.value},
                            )
                        else:
                            error = None
                except _ReadbackPersistenceError:
                    error = ErrorDetail(
                        code=ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                        message="Post-write reconciliation could not be persisted",
                        device_id=command.device_id,
                        capability=capability_name,
                        retryable=True,
                        details={"reason": "persistence_failed"},
                    )
                except (ConnectionError, OSError, TimeoutError):
                    error = ErrorDetail(
                        code=ErrorCode.POST_WRITE_RECONCILIATION_FAILED,
                        message="Post-write reconciliation read was unavailable",
                        device_id=command.device_id,
                        capability=capability_name,
                        retryable=True,
                        details={"reason": "read_unavailable"},
                    )
            if error is not None:
                self.audit.append(
                    event_type="post_write_reconciliation_failed",
                    actor="runtime",
                    subject_id=command.id,
                    payload={
                        "command_id": command.id,
                        "device_id": command.device_id,
                        "capability": capability_name,
                        "error_code": error.code,
                        "reason": error.details.get("reason", "unknown"),
                    },
                )
                return error
        self.audit.append(
            event_type="post_write_reconciliation_completed",
            actor="runtime",
            subject_id=command.id,
            payload={
                "command_id": command.id,
                "device_id": command.device_id,
                "capabilities": list(postcondition.reconcile_capabilities),
            },
        )
        return None

    async def _readback_until_settled(
        self,
        command: Command,
        capability_name: str | None,
        *,
        before_state: StateSnapshot | None = None,
    ) -> tuple[StateSnapshot | None, Exception | None]:
        postcondition = command.postconditions[0] if command.postconditions else None
        timeout = postcondition.settle_timeout_seconds if postcondition is not None else None
        poll_interval = postcondition.poll_interval_seconds if postcondition is not None else 0.0
        deadline = self.clock.now() + timedelta(seconds=timeout or 0.0)
        latest: StateSnapshot | None = None
        latest_error: Exception | None = None

        while True:
            try:
                current = await self._readback(command, capability_name)
            except (
                ConnectionError,
                OSError,
                TimeoutError,
                _ReadbackPersistenceError,
            ) as error:
                latest_error = error
            else:
                latest_error = None
                if current is not None:
                    latest = current
                if self._postcondition_matches(
                    command, capability_name, current, before_state=before_state
                ):
                    return current, None

            if timeout is None or timeout <= 0 or self.clock.now() >= deadline:
                return latest, latest_error
            remaining = (deadline - self.clock.now()).total_seconds()
            if remaining <= 0:
                return latest, latest_error
            await self._sleep(min(poll_interval, remaining))

    @staticmethod
    def _postcondition_matches(
        command: Command,
        capability_name: str | None,
        after_state: StateSnapshot | None,
        *,
        before_state: StateSnapshot | None = None,
    ) -> bool:
        if after_state is None or after_state.status is not StateStatus.CURRENT:
            return False
        if command.postconditions:
            postcondition = command.postconditions[0]
            if after_state.capability != postcondition.capability:
                return False
            if postcondition.verification == "unconfirmed":
                return False
            if postcondition.verification == "toggle_transition":
                return (
                    before_state is not None
                    and before_state.capability == after_state.capability
                    and before_state.status is StateStatus.CURRENT
                    and before_state.value != after_state.value
                )
            if postcondition.verification == "motion_stopped":
                return after_state.value is False
            if postcondition.tolerance is not None:
                actual = after_state.value
                postcondition_expected = postcondition.expected
                if (
                    not isinstance(actual, (int, float))
                    or isinstance(actual, bool)
                    or not isinstance(postcondition_expected, (int, float))
                    or isinstance(postcondition_expected, bool)
                ):
                    return False
                return (
                    abs(float(actual) - float(postcondition_expected))
                    <= postcondition.tolerance
                )
            return after_state.value == postcondition.expected
        expected: object
        if command.command == "turn_on":
            expected = True
        elif command.command == "turn_off":
            expected = False
        elif command.command == "open":
            expected = 100
        elif command.command == "close":
            expected = 0
        elif command.command == "stop":
            return False
        elif command.command.startswith("set_"):
            expected = command.value
        else:
            return False
        return after_state.value == expected

    @staticmethod
    def _terminal_plan_status(outcomes: list[ExecutionOutcome]) -> PlanStatus:
        statuses = {outcome.status for outcome in outcomes}
        if statuses == {ExecutionStatus.CONFIRMED_SUCCESS}:
            return PlanStatus.COMPLETED
        if statuses & {ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE}:
            return PlanStatus.UNKNOWN
        if ExecutionStatus.CONFIRMED_SUCCESS in statuses:
            return PlanStatus.PARTIALLY_FAILED
        return PlanStatus.FAILED
