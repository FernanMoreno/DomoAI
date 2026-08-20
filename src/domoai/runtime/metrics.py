"""In-process metrics collection for runtime observability (Spec 079)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from domoai.domain.models import PlanStatus, StateStatus
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.ports import AdapterPort, PlanRecordPort
from domoai.runtime.state_store import StateStore

if TYPE_CHECKING:
    from domoai.application.optimization_service import OptimizationService
    from domoai.persistence.sqlite import SQLiteDatabase
    from domoai.runtime.event_consumer import RuntimeEventConsumer
    from domoai.runtime.scheduler import Scheduler

_PLAN_STATUSES_TRACKED: dict[str, PlanStatus] = {
    "pending": PlanStatus.READY,
    "executing": PlanStatus.EXECUTING,
    "unknown": PlanStatus.UNKNOWN,
}


class RuntimeMetricsCollector:
    """Computes a live snapshot of runtime health signals on demand.

    Not a persisted or versioned domain contract -- a point-in-time
    observability view over already-live components, mirroring the
    existing `*_snapshot() -> dict[str, Any]` resource pattern in
    `mcp/resources.py`.
    """

    def __init__(
        self,
        *,
        adapter: AdapterPort,
        event_consumer: RuntimeEventConsumer,
        scheduler: Scheduler,
        state_store: StateStore,
        plan_repository: PlanRecordPort,
        database: SQLiteDatabase,
        optimization_service: OptimizationService | None = None,
    ) -> None:
        self.adapter = adapter
        self.event_consumer = event_consumer
        self.scheduler = scheduler
        self.state_store = state_store
        self.plan_repository = plan_repository
        self.database = database
        self.optimization_service = optimization_service

    async def snapshot(self) -> dict[str, Any]:
        health = await self.adapter.health()
        adapter_health: dict[str, Any] = {
            "connected": health.connected,
            "components": (
                [
                    {
                        "adapter_id": component.adapter_id,
                        "connected": component.connected,
                        "message": component.message,
                    }
                    for component in health.components
                ]
                if health.components is not None
                else []
            ),
        }

        if isinstance(self.adapter, CompositeAdapter):
            event_queue_depth = self.adapter.event_queue_depth
            dropped_events_total = self.adapter.dropped_events_total
        else:
            event_queue_depth = {"bulk": 0, "priority": 0}
            dropped_events_total = 0

        states = await self.state_store.all()
        stale_state_count = sum(1 for state in states if state.status is StateStatus.STALE)

        plans_by_status: dict[str, int] = {}
        for label, status in _PLAN_STATUSES_TRACKED.items():
            matching = await self.plan_repository.list_by_status(frozenset({status}))
            plans_by_status[label] = len(matching)

        db_metrics = self.database.metrics
        optimizer_last_wall_time_seconds = (
            self.optimization_service.last_wall_time_seconds
            if self.optimization_service is not None
            else None
        )

        return {
            "schema_version": "v1",
            "generated_at": datetime.now(UTC).isoformat(),
            "adapter_health": adapter_health,
            "event_consumer_alive": self.event_consumer.alive,
            "scheduler_alive": self.scheduler.alive,
            "event_queue_depth": event_queue_depth,
            "dropped_events_total": dropped_events_total,
            "stale_state_count": stale_state_count,
            "plans_by_status": plans_by_status,
            "optimizer_last_wall_time_seconds": optimizer_last_wall_time_seconds,
            "db_operation_count": db_metrics.operation_count,
            "db_busy_count": db_metrics.busy_count,
        }
