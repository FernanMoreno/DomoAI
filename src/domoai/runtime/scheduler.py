"""Persistent, restart-safe scheduling of approved plans."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from domoai.domain.models import Plan
from domoai.persistence.repositories import ScheduledPlanRepository
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor


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
    ) -> None:
        self.executor = executor
        self.repository = repository
        self.audit = audit
        self.grace_window = grace_window
        self.poll_interval = poll_interval

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

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval.total_seconds())
            await self.run_due()
