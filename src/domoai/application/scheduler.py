"""Persistent, restart-safe scheduling of approved plans."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from domoai.application.executor import PlanExecutor
from domoai.application.recurrence import next_occurrence
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    BundleMemberCommitStatus,
    Command,
    ExecutionStatus,
    ExecutionSummary,
    Plan,
    PlanStatus,
    RecurrenceRule,
)
from domoai.persistence.repositories import (
    BundleCommitRepository,
    RecurringScheduleRepository,
    ScheduledPlanRepository,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog


@dataclass(frozen=True)
class _PredecessorGateResult:
    allowed: bool
    predecessor_plan_id: str | None = None
    state_version_overrides: dict[str, int] = field(default_factory=dict)


class Scheduler:
    """Holds plans awaiting their execute_at and runs them via PlanExecutor."""

    _PLAN_TO_SCHEDULE_STATUS = {
        PlanStatus.COMPLETED: "executed",
        PlanStatus.FAILED: "failed",
        PlanStatus.PARTIALLY_FAILED: "failed",
        PlanStatus.UNKNOWN: "unknown",
        PlanStatus.CANCELLED: "cancelled",
    }

    def __init__(
        self,
        executor: PlanExecutor,
        repository: ScheduledPlanRepository,
        audit: AuditLog,
        *,
        grace_window: timedelta = timedelta(seconds=900),
        poll_interval: timedelta = timedelta(seconds=30),
        recurring_repository: RecurringScheduleRepository | None = None,
        bundle_repository: BundleCommitRepository | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.executor = executor
        self.repository = repository
        self.audit = audit
        self.grace_window = grace_window
        self.poll_interval = poll_interval
        self.recurring_repository = recurring_repository
        self.bundle_repository = bundle_repository
        self.clock = clock or SystemClock()
        self.alive = False
        self.last_lateness_seconds: float | None = None
        self.max_lateness_seconds = 0.0
        self.missed_total = 0
        self.execution_unknown_total = 0
        self.execution_unavailable_total = 0
        self.execution_failed_total = 0
        self.execution_partial_total = 0

    async def schedule(self, plan: Plan) -> None:
        await self.repository.schedule(plan)

    async def cancel(self, plan_id: str) -> bool:
        return await self.repository.cancel(plan_id)

    async def reschedule(
        self,
        plan_id: str,
        execute_at: datetime,
        *,
        expected_revision: int | None = None,
        expected_validation_digest: str | None = None,
        replacement_plan: Plan | None = None,
    ) -> bool:
        return await self.repository.reschedule(
            plan_id,
            execute_at,
            expected_revision=expected_revision,
            expected_validation_digest=expected_validation_digest,
            replacement_plan=replacement_plan,
        )

    async def list_pending(self) -> list[Plan]:
        return await self.repository.list_pending()

    def _plan_repository(self) -> Any | None:
        return getattr(self.executor, "plan_repository", None)

    @classmethod
    def _schedule_status_for_plan(cls, status: PlanStatus) -> str | None:
        return cls._PLAN_TO_SCHEDULE_STATUS.get(status)

    async def _terminal_plan_evidence(self, plan_id: str) -> tuple[PlanStatus, str] | None:
        plan_repository = self._plan_repository()
        if plan_repository is None:
            return None
        persisted = await plan_repository.get(plan_id)
        if persisted is None:
            return None
        schedule_status = self._schedule_status_for_plan(persisted.status)
        if schedule_status is None:
            return None
        return persisted.status, schedule_status

    async def _reconcile_scheduled_plan(self, plan_id: str) -> str | None:
        evidence = await self._terminal_plan_evidence(plan_id)
        if evidence is None:
            return None
        plan_status, schedule_status = evidence
        if not await self.repository.reconcile_terminal(plan_id, schedule_status):
            raise RuntimeError(f"Could not reconcile scheduled plan {plan_id}")
        self.audit.append(
            event_type="schedule_execution_reconciled",
            actor="runtime",
            subject_id=plan_id,
            payload={
                "plan_status": plan_status.value,
                "schedule_status": schedule_status,
            },
        )
        return schedule_status

    async def _reconcile_inflight_plan(
        self, plan_id: str, *, settle_scheduled_row: bool = True
    ) -> bool:
        """Turn orphaned in-flight evidence into non-replayable UNKNOWN.

        The scheduler is a single owner of its SQLite runtime. Seeing an
        ``EXECUTING`` plan while its scheduled row is still pending therefore
        means the previous execution crossed the physical-write boundary but
        did not settle durably. It is uncertainty evidence, never permission
        to call the adapter again.
        """

        plan_repository = self._plan_repository()
        if plan_repository is None:
            return False
        persisted = await plan_repository.get(plan_id)
        if persisted is None or persisted.status is not PlanStatus.EXECUTING:
            return False
        if not await plan_repository.mark_unknown_if_executing(plan_id):
            return False
        if settle_scheduled_row and not await self.repository.reconcile_terminal(
            plan_id, "unknown"
        ):
            raise RuntimeError(f"Could not settle uncertain scheduled plan {plan_id}")
        self.audit.append(
            event_type="schedule_execution_reconciled",
            actor="runtime",
            subject_id=plan_id,
            payload={
                "plan_status": PlanStatus.EXECUTING.value,
                "schedule_status": "unknown",
                "reason": "orphaned_inflight_evidence",
            },
        )
        return True

    async def _complete_scheduled_plan(
        self, plan_id: str, execution: ExecutionSummary | None = None
    ) -> str:
        plan_repository = self._plan_repository()
        if plan_repository is None:
            derived_schedule_status = "executed"
            if execution is not None:
                statuses = {outcome.status for outcome in execution.outcomes}
                if statuses & {ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE}:
                    derived_schedule_status = "unknown"
                elif statuses != {ExecutionStatus.CONFIRMED_SUCCESS}:
                    derived_schedule_status = "failed"
            if derived_schedule_status == "executed":
                settled = await self.repository.mark_executed(plan_id)
            else:
                settled = await self.repository.reconcile_terminal(plan_id, derived_schedule_status)
            if not settled:
                raise RuntimeError(f"Could not settle scheduled plan {plan_id}")
            return derived_schedule_status
        persisted = await plan_repository.get(plan_id)
        if persisted is None:
            raise RuntimeError("Execution returned without a persisted plan")
        schedule_status = self._schedule_status_for_plan(persisted.status)
        if schedule_status is None:
            raise RuntimeError(
                f"Execution returned with non-terminal persisted status {persisted.status.value}"
            )
        transition_succeeded = (
            await self.repository.mark_executed(plan_id)
            if schedule_status == "executed"
            else await self.repository.reconcile_terminal(plan_id, schedule_status)
        )
        if not transition_succeeded:
            raise RuntimeError(f"Could not settle scheduled plan {plan_id}")
        return schedule_status

    async def _assert_schedule_evidence(self, scheduled_plan: Plan) -> None:
        """Verify durable temporal evidence before crossing into execution."""

        persisted_schedule = await self.repository.get(scheduled_plan.id)
        if persisted_schedule is None or persisted_schedule[1] != "pending":
            raise DomainError(
                ErrorCode.SCHEDULE_EVIDENCE_MISMATCH,
                "Scheduled plan is no longer pending",
            )
        stored, _status = persisted_schedule
        if (
            stored.execute_at != scheduled_plan.execute_at
            or stored.schedule_revision != scheduled_plan.schedule_revision
            or (stored.execution_window.digest if stored.execution_window else None)
            != (scheduled_plan.execution_window.digest if scheduled_plan.execution_window else None)
            or (stored.validation.digest if stored.validation else None)
            != (scheduled_plan.validation.digest if scheduled_plan.validation else None)
        ):
            raise DomainError(
                ErrorCode.SCHEDULE_EVIDENCE_MISMATCH,
                "Scheduled row evidence differs from the claimed plan",
            )

        plan_repository = self._plan_repository()
        if plan_repository is None:
            return
        persisted_plan = await plan_repository.get(scheduled_plan.id)
        if persisted_plan is None:
            return
        if (
            persisted_plan.execute_at != scheduled_plan.execute_at
            or persisted_plan.schedule_revision != scheduled_plan.schedule_revision
            or (persisted_plan.execution_window.digest if persisted_plan.execution_window else None)
            != (scheduled_plan.execution_window.digest if scheduled_plan.execution_window else None)
            or (persisted_plan.validation.digest if persisted_plan.validation else None)
            != (scheduled_plan.validation.digest if scheduled_plan.validation else None)
        ):
            raise DomainError(
                ErrorCode.SCHEDULE_EVIDENCE_MISMATCH,
                "Plan repository evidence differs from the scheduled intent",
            )

    async def _predecessor_gate(self, plan: Plan) -> _PredecessorGateResult:
        """Decide whether ``plan`` may be dispatched to the adapter at all.

        A scheduled member with a ``predecessor_plan_id`` represents a
        physical trajectory the optimizer computed assuming its predecessor
        actually happened (e.g. a battery discharge plan that assumes an
        earlier charge slot executed). If the predecessor did not reach
        confirmed_success, dispatching the dependent command would act on a
        state the optimizer never actually validated. ``allowed=False`` MUST
        result in zero adapter calls for this plan.
        """
        if self.bundle_repository is None:
            return _PredecessorGateResult(allowed=True)
        bundle = await self.bundle_repository.get_for_plan(plan.id)
        if bundle is None:
            return _PredecessorGateResult(allowed=True)
        member = next((item for item in bundle.members if item.plan_id == plan.id), None)
        if member is None or member.predecessor_plan_id is None:
            return _PredecessorGateResult(allowed=True)
        predecessor_plan_id = member.predecessor_plan_id
        predecessor = next(
            (item for item in bundle.members if item.plan_id == predecessor_plan_id),
            None,
        )
        if predecessor is None or predecessor.status is not BundleMemberCommitStatus.EXECUTED:
            return _PredecessorGateResult(
                allowed=False, predecessor_plan_id=predecessor_plan_id
            )
        evidence = predecessor.details.get("dependency_evidence")
        if not isinstance(evidence, dict) or (
            evidence.get("status") != ExecutionStatus.CONFIRMED_SUCCESS.value
        ):
            return _PredecessorGateResult(
                allowed=False, predecessor_plan_id=predecessor_plan_id
            )
        overrides: dict[str, int] = {}
        versions = evidence.get("state_versions")
        dependencies = plan.validation.dependencies if plan.validation is not None else None
        if isinstance(versions, dict) and dependencies is not None:
            overrides = {
                key: value
                for key, value in versions.items()
                if key in dependencies.state_versions and isinstance(value, int)
            }
        return _PredecessorGateResult(
            allowed=True,
            predecessor_plan_id=predecessor_plan_id,
            state_version_overrides=overrides,
        )

    async def _record_bundle_outcome(
        self, plan: Plan, execution: ExecutionSummary
    ) -> None:
        if self.bundle_repository is None:
            return
        statuses = {outcome.status for outcome in execution.outcomes}
        if statuses == {ExecutionStatus.CONFIRMED_SUCCESS}:
            member_status = BundleMemberCommitStatus.EXECUTED
            execution_status = ExecutionStatus.CONFIRMED_SUCCESS
        elif statuses & {ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE}:
            member_status = BundleMemberCommitStatus.UNKNOWN
            execution_status = next(
                status
                for status in (ExecutionStatus.UNKNOWN, ExecutionStatus.UNAVAILABLE)
                if status in statuses
            )
        else:
            member_status = BundleMemberCommitStatus.FAILED
            execution_status = next(iter(statuses), ExecutionStatus.FAILED)
        state_store = getattr(self.executor.plan_service, "state_store", None)
        state_versions: dict[str, int] = {}
        if state_store is not None:
            keys: set[str] = set()
            if plan.validation and plan.validation.dependencies:
                keys.update(plan.validation.dependencies.state_versions)
            for command in plan.commands:
                capability = self.executor.plan_service.capability_for_command(command)
                if capability is not None:
                    keys.add(f"{command.device_id}::{capability.name}")
            for key in keys:
                device_id, _, capability_name = key.partition("::")
                state_versions[key] = state_store.state_version(device_id, capability_name)
        details = {
            "dependency_evidence": {
                "predecessor_plan_id": plan.id,
                "status": execution_status.value,
                "state_versions": state_versions,
            }
        }
        await self.bundle_repository.record_member_outcome(
            plan.id,
            status=member_status,
            execution_status=execution_status,
            details=details,
        )

    async def run_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        sweep_time = now or self.clock.now()
        results: list[dict[str, Any]] = []
        for plan in await self.repository.list_pending():
            assert plan.execute_at is not None
            if plan.execute_at > sweep_time:
                continue
            try:
                reconciled_status = await self._reconcile_scheduled_plan(plan.id)
            except Exception as error:
                self.audit.append(
                    event_type="schedule_reconciliation_error",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={"error": str(error)[:200]},
                )
                results.append({"plan_id": plan.id, "outcome": "error"})
                continue
            if reconciled_status is not None:
                results.append({"plan_id": plan.id, "outcome": "reconciled"})
                continue
            try:
                if await self._reconcile_inflight_plan(plan.id):
                    results.append({"plan_id": plan.id, "outcome": "reconciled"})
                    continue
            except Exception as error:
                self.audit.append(
                    event_type="schedule_reconciliation_error",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={"error": str(error)[:200]},
                )
                results.append({"plan_id": plan.id, "outcome": "error"})
                continue
            overdue = sweep_time - plan.execute_at
            lateness = max(0.0, overdue.total_seconds())
            self.last_lateness_seconds = lateness
            self.max_lateness_seconds = max(self.max_lateness_seconds, lateness)
            if overdue > self.grace_window:
                self.missed_total += 1
                await self.repository.mark_missed(plan.id)
                if self.bundle_repository is not None:
                    await self.bundle_repository.record_member_outcome(
                        plan.id,
                        status=BundleMemberCommitStatus.MISSED,
                        execution_status=None,
                        details={
                            "reason": "outside_scheduler_grace_window",
                            "execute_at": plan.execute_at.isoformat(),
                        },
                    )
                self.audit.append(
                    event_type="schedule_missed",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={
                        "execute_at": plan.execute_at.isoformat(),
                        "overdue_seconds": overdue.total_seconds(),
                        "grace_window_seconds": self.grace_window.total_seconds(),
                    },
                )
                results.append({"plan_id": plan.id, "outcome": "missed"})
                continue
            try:
                await self._assert_schedule_evidence(plan)
                gate = await self._predecessor_gate(plan)
                if not gate.allowed:
                    await self.repository.reconcile_terminal(plan.id, "failed")
                    if self.bundle_repository is not None:
                        await self.bundle_repository.record_member_outcome(
                            plan.id,
                            status=BundleMemberCommitStatus.DEPENDENCY_FAILED,
                            execution_status=None,
                            details={
                                "reason": "predecessor_not_confirmed_success",
                                "predecessor_plan_id": gate.predecessor_plan_id,
                            },
                        )
                    self.audit.append(
                        event_type="schedule_dependency_failed",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={"predecessor_plan_id": gate.predecessor_plan_id},
                    )
                    results.append({"plan_id": plan.id, "outcome": "dependency_failed"})
                    continue
                if gate.state_version_overrides:
                    execution = await self.executor.execute(
                        plan, state_version_overrides=gate.state_version_overrides
                    )
                else:
                    execution = await self.executor.execute(plan)
                statuses = {outcome.status for outcome in execution.outcomes}
                if ExecutionStatus.UNKNOWN in statuses:
                    self.execution_unknown_total += 1
                if ExecutionStatus.UNAVAILABLE in statuses:
                    self.execution_unavailable_total += 1
                if statuses and statuses != {ExecutionStatus.CONFIRMED_SUCCESS}:
                    self.execution_failed_total += 1
                if len(statuses) > 1:
                    self.execution_partial_total += 1
                schedule_outcome = await self._complete_scheduled_plan(plan.id, execution)
                await self._record_bundle_outcome(plan, execution)
            except DomainError as error:
                if error.code is ErrorCode.SCHEDULE_EVIDENCE_MISMATCH:
                    await self.repository.reconcile_terminal(plan.id, "failed")
                    self.audit.append(
                        event_type="schedule_evidence_rejected",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={"error_code": error.code.value},
                    )
                    results.append({"plan_id": plan.id, "outcome": "failed"})
                    continue
                self.audit.append(
                    event_type="schedule_execution_error",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={
                        "error": str(error)[:200],
                        "error_code": error.code.value,
                    },
                )
                results.append({"plan_id": plan.id, "outcome": "error"})
                continue
            except Exception as error:
                try:
                    reconciled_status = await self._reconcile_scheduled_plan(plan.id)
                except Exception as reconciliation_error:
                    reconciled_status = None
                    self.audit.append(
                        event_type="schedule_reconciliation_error",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={"error": str(reconciliation_error)[:200]},
                    )
                if reconciled_status is not None:
                    results.append({"plan_id": plan.id, "outcome": "reconciled"})
                    continue
                self.audit.append(
                    event_type="schedule_execution_error",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={
                        "error": str(error)[:200],
                        "error_code": getattr(error, "code", None),
                    },
                )
                results.append({"plan_id": plan.id, "outcome": "error"})
                continue
            results.append({"plan_id": plan.id, "outcome": schedule_outcome})
        return results

    async def schedule_recurring(
        self,
        schedule_id: str,
        commands: list[Command],
        rule: RecurrenceRule,
        *,
        plan: Plan | None = None,
        approval: Any | None = None,
    ) -> datetime:
        if self.recurring_repository is None:
            raise ValueError("Recurring scheduling is unavailable in this deployment")
        first_occurrence = next_occurrence(rule, self.clock.now())
        if rule.expires_at is not None and first_occurrence > rule.expires_at:
            raise ValueError("Recurring schedule expires_at is before its first occurrence")
        await self.recurring_repository.create(schedule_id, commands, rule, first_occurrence)
        # Creating a standing automation is a distinct authority act from
        # executing a command once (see spec 145): the digest, policy
        # decisions, and (when required) the approval evidence that
        # justified it are recorded durably here, not just at each
        # occurrence's own JIT re-validation.
        self.audit.append(
            event_type="recurring_schedule_created",
            actor="runtime",
            subject_id=schedule_id,
            payload={
                "requires_confirmation": (
                    plan.status is PlanStatus.REQUIRES_CONFIRMATION if plan is not None else None
                ),
                "validation_digest": (
                    plan.validation.digest if plan is not None and plan.validation else None
                ),
                "policy_decisions": (
                    [decision.model_dump(mode="json") for decision in plan.policy_decisions]
                    if plan is not None
                    else []
                ),
                "approval_id": getattr(approval, "approval_id", None),
                "approved_by": getattr(approval, "approved_by", None),
                "expires_at": rule.expires_at.isoformat() if rule.expires_at else None,
            },
        )
        return first_occurrence

    async def cancel_recurring(self, schedule_id: str) -> bool:
        if self.recurring_repository is None:
            raise ValueError("Recurring scheduling is unavailable in this deployment")
        return await self.recurring_repository.cancel(schedule_id)

    async def list_recurring(self) -> list[tuple[str, list[Command], RecurrenceRule, datetime]]:
        if self.recurring_repository is None:
            raise ValueError("Recurring scheduling is unavailable in this deployment")
        return await self.recurring_repository.list_active()

    async def run_due_recurring(self, now: datetime | None = None) -> list[dict[str, Any]]:
        if self.recurring_repository is None:
            return []
        sweep_time = now or self.clock.now()
        results: list[dict[str, Any]] = []
        active_schedules = await self.recurring_repository.list_active()
        for schedule_id, commands, rule, execute_at in active_schedules:
            if rule.expires_at is not None and execute_at > rule.expires_at:
                await self.recurring_repository.cancel(schedule_id)
                self.audit.append(
                    event_type="recurring_schedule_expired",
                    actor="runtime",
                    subject_id=schedule_id,
                    payload={"expires_at": rule.expires_at.isoformat()},
                )
                results.append({"schedule_id": schedule_id, "outcome": "expired"})
                continue
            if execute_at > sweep_time:
                continue
            plan = Plan(
                id=f"{schedule_id}@{execute_at.isoformat()}",
                commands=commands,
                created_at=self.clock.now(),
            )
            try:
                if await self._reconcile_inflight_plan(plan.id, settle_scheduled_row=False):
                    next_time = next_occurrence(rule, execute_at)
                    await self.recurring_repository.advance(schedule_id, next_time)
                    self.audit.append(
                        event_type="recurring_occurrence_reconciled",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={
                            "schedule_id": schedule_id,
                            "plan_status": PlanStatus.EXECUTING.value,
                            "reason": "orphaned_inflight_evidence",
                        },
                    )
                    results.append({"schedule_id": schedule_id, "outcome": "reconciled"})
                    continue
                evidence = await self._terminal_plan_evidence(plan.id)
                if evidence is not None:
                    plan_status, _schedule_status = evidence
                    next_time = next_occurrence(rule, execute_at)
                    await self.recurring_repository.advance(schedule_id, next_time)
                    self.audit.append(
                        event_type="recurring_occurrence_reconciled",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={
                            "schedule_id": schedule_id,
                            "plan_status": plan_status.value,
                        },
                    )
                    results.append({"schedule_id": schedule_id, "outcome": "reconciled"})
                    continue
                validated = self.executor.plan_service.validate(plan)
                if validated.status is PlanStatus.READY:
                    await self.executor.execute(validated)
                    outcome = "executed"
                else:
                    reason = (
                        "requires_confirmation"
                        if validated.status is PlanStatus.REQUIRES_CONFIRMATION
                        else "invalid"
                    )
                    self.audit.append(
                        event_type="recurring_occurrence_skipped",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={"schedule_id": schedule_id, "reason": reason},
                    )
                    outcome = "skipped"
                next_time = next_occurrence(rule, execute_at)
                await self.recurring_repository.advance(schedule_id, next_time)
            except Exception as error:
                self.audit.append(
                    event_type="recurring_occurrence_error",
                    actor="runtime",
                    subject_id=plan.id,
                    payload={
                        "schedule_id": schedule_id,
                        "error": str(error)[:200],
                        "error_code": getattr(error, "code", None),
                    },
                )
                results.append({"schedule_id": schedule_id, "outcome": "error"})
                continue
            results.append({"schedule_id": schedule_id, "outcome": outcome})
        return results

    async def run(self) -> None:
        self.alive = True
        try:
            while True:
                await asyncio.sleep(self.poll_interval.total_seconds())
                try:
                    await self.run_due()
                except Exception as error:
                    self.audit.append(
                        event_type="schedule_sweep_error",
                        actor="runtime",
                        subject_id="scheduler",
                        payload={"error": str(error)[:200]},
                    )
                try:
                    await self.run_due_recurring()
                except Exception as error:
                    self.audit.append(
                        event_type="recurring_sweep_error",
                        actor="runtime",
                        subject_id="scheduler",
                        payload={"error": str(error)[:200]},
                    )
        finally:
            self.alive = False
