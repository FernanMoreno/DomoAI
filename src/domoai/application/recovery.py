"""Startup recovery for plans orphaned by a crash mid-execution."""

from __future__ import annotations

from domoai.domain.models import PlanStatus
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import AuditLog
from domoai.runtime.ports import PlanRecordPort


class PlanRecoveryService:
    """Reconcile executing plans only within a verified recovery scope."""

    def __init__(
        self,
        plan_repository: PlanRecordPort,
        audit: AuditLog,
        *,
        deployment_id: str | None = None,
        fencing_epoch: int | None = None,
        clock: Clock | None = None,
    ) -> None:
        if fencing_epoch is not None and fencing_epoch <= 0:
            raise ValueError("fencing_epoch must be positive")
        if fencing_epoch is not None and deployment_id is None:
            raise ValueError("fencing recovery requires a deployment scope")
        self.plan_repository = plan_repository
        self.audit = audit
        self.deployment_id = deployment_id
        self.fencing_epoch = fencing_epoch
        self.clock = clock or SystemClock()

    async def reconcile(self, *, reason: str = "periodic_recovery") -> list[str]:
        orphaned = await self.plan_repository.list_by_status(
            frozenset({PlanStatus.EXECUTING}),
            deployment_id=self.deployment_id,
            include_legacy=self.fencing_epoch is None,
        )
        recovered_ids: list[str] = []
        for plan in orphaned:
            lease = plan.execution_lease
            if self.fencing_epoch is not None:
                if lease is None:
                    self.audit.append(
                        event_type="plan_execution_recovery_skipped",
                        actor="runtime",
                        subject_id=plan.id,
                        payload={
                            "reason": "missing_fencing_evidence",
                            "deployment_id": self.deployment_id,
                        },
                    )
                    continue
                now = self.clock.now()
                if (
                    lease.deployment_id != self.deployment_id
                    or lease.expires_at > now
                    or lease.fencing_epoch >= self.fencing_epoch
                ):
                    continue
            if not await self.plan_repository.mark_unknown_if_executing(
                plan.id,
                expected_execution_lease=lease if self.fencing_epoch is not None else None,
                allow_legacy=self.fencing_epoch is None,
            ):
                continue
            self.audit.append(
                event_type="plan_execution_recovered",
                actor="runtime",
                subject_id=plan.id,
                payload={
                    "reason": reason,
                    "previous_status": PlanStatus.EXECUTING.value,
                    "deployment_id": self.deployment_id,
                    "fencing_epoch": self.fencing_epoch,
                },
            )
            recovered_ids.append(plan.id)
        return recovered_ids

    async def recover_orphaned_plans(self) -> list[str]:
        """Compatibility entry point for the startup reconciliation pass."""

        return await self.reconcile(reason="startup_crash_recovery")
