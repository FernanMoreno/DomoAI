"""Persistent, restart-safe scheduling of approved plans."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from domoai.domain.models import Command, Plan, PlanStatus, RecurrenceRule
from domoai.persistence.repositories import RecurringScheduleRepository, ScheduledPlanRepository
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.recurrence import next_occurrence


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
        clock: Clock | None = None,
    ) -> None:
        self.executor = executor
        self.repository = repository
        self.audit = audit
        self.grace_window = grace_window
        self.poll_interval = poll_interval
        self.recurring_repository = recurring_repository
        self.clock = clock or SystemClock()
        self.alive = False

    async def schedule(self, plan: Plan) -> None:
        await self.repository.schedule(plan)

    async def cancel(self, plan_id: str) -> bool:
        return await self.repository.cancel(plan_id)

    async def reschedule(self, plan_id: str, execute_at: datetime) -> bool:
        return await self.repository.reschedule(plan_id, execute_at)

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

    async def _complete_scheduled_plan(self, plan_id: str) -> str:
        plan_repository = self._plan_repository()
        if plan_repository is None:
            if not await self.repository.mark_executed(plan_id):
                raise RuntimeError(f"Could not settle scheduled plan {plan_id}")
            return "executed"
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
            if overdue > self.grace_window:
                await self.repository.mark_missed(plan.id)
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
                await self.executor.execute(plan)
                await self._complete_scheduled_plan(plan.id)
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
            results.append({"plan_id": plan.id, "outcome": "executed"})
        return results

    async def schedule_recurring(
        self, schedule_id: str, commands: list[Command], rule: RecurrenceRule
    ) -> datetime:
        if self.recurring_repository is None:
            raise ValueError("Recurring scheduling is unavailable in this deployment")
        first_occurrence = next_occurrence(rule, self.clock.now())
        await self.recurring_repository.create(schedule_id, commands, rule, first_occurrence)
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
