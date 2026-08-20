"""Startup recovery for plans orphaned by a crash mid-execution."""

from __future__ import annotations

from domoai.domain.models import PlanStatus
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import PlanRecordPort


class PlanRecoveryService:
    """Resolves plans left stuck in EXECUTING by a crash to a safe terminal status.

    Assumes a single-process runtime: any plan found in EXECUTING status at
    the time this runs is orphaned, since no live execution loop from a
    previous process can still be holding it.
    """

    def __init__(self, plan_repository: PlanRecordPort, audit: AuditLog) -> None:
        self.plan_repository = plan_repository
        self.audit = audit

    async def recover_orphaned_plans(self) -> list[str]:
        orphaned = await self.plan_repository.list_by_status(
            frozenset({PlanStatus.EXECUTING})
        )
        recovered_ids: list[str] = []
        for plan in orphaned:
            await self.plan_repository.save(
                plan.model_copy(update={"status": PlanStatus.UNKNOWN})
            )
            self.audit.append(
                event_type="plan_execution_recovered",
                actor="runtime",
                subject_id=plan.id,
                payload={
                    "reason": "startup_crash_recovery",
                    "previous_status": PlanStatus.EXECUTING.value,
                },
            )
            recovered_ids.append(plan.id)
        return recovered_ids
