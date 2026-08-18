"""Persistent, restart-safe scheduling of approved plans."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from domoai.domain.models import Command, Plan, PlanStatus, RecurrenceRule
from domoai.persistence.repositories import RecurringScheduleRepository, ScheduledPlanRepository
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.recurrence import next_occurrence


class Scheduler:
    """Holds plans awaiting their execute_at and runs them via PlanExecutor."""

    def __init__(
        self,
        executor: PlanExecutor,
        repository: ScheduledPlanRepository,
        audit: AuditLog,
        *,
        grace_window: timedelta = timedelta(seconds=900),
        poll_interval: timedelta = timedelta(seconds=30),
        recurring_repository: RecurringScheduleRepository | None = None,
    ) -> None:
        self.executor = executor
        self.repository = repository
        self.audit = audit
        self.grace_window = grace_window
        self.poll_interval = poll_interval
        self.recurring_repository = recurring_repository

    async def schedule(self, plan: Plan) -> None:
        await self.repository.schedule(plan)

    async def cancel(self, plan_id: str) -> bool:
        return await self.repository.cancel(plan_id)

    async def reschedule(self, plan_id: str, execute_at: datetime) -> bool:
        return await self.repository.reschedule(plan_id, execute_at)

    async def list_pending(self) -> list[Plan]:
        return await self.repository.list_pending()

    async def run_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        sweep_time = now or datetime.now(UTC)
        results: list[dict[str, Any]] = []
        for plan in await self.repository.list_pending():
            assert plan.execute_at is not None
            if plan.execute_at > sweep_time:
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
            await self.executor.execute(plan)
            await self.repository.mark_executed(plan.id)
            results.append({"plan_id": plan.id, "outcome": "executed"})
        return results

    async def schedule_recurring(
        self, schedule_id: str, commands: list[Command], rule: RecurrenceRule
    ) -> datetime:
        if self.recurring_repository is None:
            raise ValueError("Recurring scheduling is unavailable in this deployment")
        first_occurrence = next_occurrence(rule, datetime.now(UTC))
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
        sweep_time = now or datetime.now(UTC)
        results: list[dict[str, Any]] = []
        active_schedules = await self.recurring_repository.list_active()
        for schedule_id, commands, rule, execute_at in active_schedules:
            if execute_at > sweep_time:
                continue
            plan = Plan(
                id=f"{schedule_id}@{execute_at.isoformat()}",
                commands=commands,
            )
            validated = self.executor.plan_service.validate(plan)
            if validated.status is PlanStatus.READY:
                await self.executor.execute(validated)
                results.append({"schedule_id": schedule_id, "outcome": "executed"})
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
                results.append({"schedule_id": schedule_id, "outcome": "skipped"})
            next_time = next_occurrence(rule, execute_at)
            await self.recurring_repository.advance(schedule_id, next_time)
        return results

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval.total_seconds())
            await self.run_due()
            await self.run_due_recurring()
