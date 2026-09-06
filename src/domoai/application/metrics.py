"""In-process metrics collection for runtime observability (Spec 079)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from domoai.domain.models import PlanStatus, StateStatus
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.operational_metrics import RuntimeOperationalMetrics
from domoai.runtime.ports import AdapterPort, PlanRecordPort
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore

if TYPE_CHECKING:
    from domoai.application.event_consumer import RuntimeEventConsumer
    from domoai.application.household_queue import HouseholdWorkQueues
    from domoai.application.optimization_service import OptimizationService
    from domoai.application.scheduler import Scheduler
    from domoai.persistence.coordination import MetricHistoryRepository
    from domoai.persistence.serialized import SerializedStorageExecutor, StorageMetrics
    from domoai.persistence.sqlite import SQLiteDatabase
    from domoai.runtime.events import AuditLog


class OptimizerWorkerLike(Protocol):
    """Shape both `OptimizationWorker` (thread-backed) and
    `ProcessOptimizationWorker` (spec 150, process-backed) satisfy --
    referenced structurally so this module doesn't need to import either
    concrete class."""

    last_wall_time_seconds: float | None
    last_queue_wait_seconds: float | None

_PLAN_STATUSES_TRACKED: dict[str, PlanStatus] = {
    "pending": PlanStatus.READY,
    "executing": PlanStatus.EXECUTING,
    "unknown": PlanStatus.UNKNOWN,
}


def _storage_metrics_dict(storage_metrics: StorageMetrics | None) -> dict[str, Any] | None:
    if storage_metrics is None:
        return None
    return {
        "operation_count": storage_metrics.operation_count,
        "completed_count": storage_metrics.completed_count,
        "failed_count": storage_metrics.failed_count,
        "timeout_count": storage_metrics.timeout_count,
        "overloaded_count": storage_metrics.overloaded_count,
        "queue_depth": storage_metrics.queue_depth,
        "max_in_flight": storage_metrics.max_in_flight,
        "total_queue_wait_seconds": storage_metrics.total_queue_wait_seconds,
        "last_error": storage_metrics.last_error,
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
        registry: DeviceRegistry | None = None,
        event_consumer: RuntimeEventConsumer,
        scheduler: Scheduler,
        state_store: StateStore,
        plan_repository: PlanRecordPort,
        database: SQLiteDatabase,
        storage: SerializedStorageExecutor | None = None,
        audit_storage: SerializedStorageExecutor | None = None,
        audit: AuditLog | None = None,
        battery_qualification: str = "unsupported",
        optimization_worker: OptimizerWorkerLike | None = None,
        optimization_service: OptimizationService | None = None,
        clock: Clock | None = None,
        operational_metrics: RuntimeOperationalMetrics | None = None,
        metric_history_repository: MetricHistoryRepository | None = None,
        metric_history_max_samples: int = 1000,
        household_work_queues: HouseholdWorkQueues | None = None,
    ) -> None:
        self.adapter = adapter
        self.registry = registry
        self.event_consumer = event_consumer
        self.scheduler = scheduler
        self.state_store = state_store
        self.plan_repository = plan_repository
        self.database = database
        self.storage = storage
        self.audit_storage = audit_storage
        self.audit = audit
        self.battery_qualification = battery_qualification
        self.optimization_worker = optimization_worker
        self.optimization_service = optimization_service
        self.clock = clock or SystemClock()
        self.operational_metrics = operational_metrics or RuntimeOperationalMetrics()
        self.metric_history_repository = metric_history_repository
        self.metric_history_max_samples = metric_history_max_samples
        self.household_work_queues = household_work_queues

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
            dropped_events_by_adapter = self.adapter.dropped_events_by_adapter
            dropped_events_by_kind = self.adapter.dropped_events_by_kind
            coalesced_events_total = self.adapter.coalesced_events_total
            reconnect_metrics = self.adapter.reconnect_metrics
        else:
            event_queue_depth = {"bulk": 0, "priority": 0}
            dropped_events_total = 0
            dropped_events_by_adapter = {}
            dropped_events_by_kind = {}
            coalesced_events_total = 0
            reconnect_metrics = {
                "attempts_total": 0,
                "success_total": 0,
                "failure_total": 0,
            }

        states = await self.state_store.all()
        stale_state_count = sum(1 for state in states if state.status is StateStatus.STALE)
        state_quality_by_source: dict[str, dict[str, int]] = {}
        for state in states:
            source_id = state.source_ref.adapter_id
            quality = state_quality_by_source.setdefault(
                source_id,
                {status.value: 0 for status in StateStatus},
            )
            quality[state.status.value] += 1

        plans_by_status: dict[str, int] = {}
        for label, status in _PLAN_STATUSES_TRACKED.items():
            matching = await self.plan_repository.list_by_status(frozenset({status}))
            plans_by_status[label] = len(matching)

        db_metrics = self.database.metrics
        storage_metrics = self.storage.metrics if self.storage is not None else None
        audit_storage_metrics = (
            self.audit_storage.metrics if self.audit_storage is not None else None
        )
        max_state_age_seconds = self.state_store.max_state_age_seconds(self.clock.now())
        active_providers = self._active_provider_ids(self.adapter)
        writable_capabilities = self._writable_capabilities()
        # Prefer the worker's own tracking (spec 150: ProcessOptimizationWorker
        # bypasses OptimizationService.optimize entirely, so it never updates
        # OptimizationService.last_wall_time_seconds; the thread-backed
        # OptimizationWorker still routes through the service, so this falls
        # back to it for that path / for callers that only wire a service).
        optimizer_last_wall_time_seconds = (
            self.optimization_worker.last_wall_time_seconds
            if self.optimization_worker is not None
            else (
                self.optimization_service.last_wall_time_seconds
                if self.optimization_service is not None
                else None
            )
        )

        operational = self.operational_metrics.snapshot()
        operational["state_quality_by_source"] = state_quality_by_source
        if self.metric_history_repository is not None:
            try:
                process_start_time = datetime.fromisoformat(operational["process_start_time"])
                for metric_name in ("event_count", "stale_state_count", "db_operation_count"):
                    await self.metric_history_repository.append(
                        instance_id=operational["instance_id"],
                        process_start_time=process_start_time,
                        metric_name=metric_name,
                        value=float(
                            {
                                "event_count": self.event_consumer.events_applied,
                                "stale_state_count": stale_state_count,
                                "db_operation_count": db_metrics.operation_count,
                            }[metric_name]
                        ),
                        max_samples=self.metric_history_max_samples,
                    )
            except (OSError, TypeError, ValueError):
                self.operational_metrics.record_telemetry_failure()

        return {
            "schema_version": "v1",
            "instance_id": operational["instance_id"],
            "process_start_time_seconds": operational["process_start_time_seconds"],
            "generated_at": self.clock.now().isoformat(),
            "adapter_health": adapter_health,
            "event_consumer_alive": self.event_consumer.alive,
            "scheduler_alive": self.scheduler.alive,
            "event_queue_depth": event_queue_depth,
            "dropped_events_total": dropped_events_total,
            "dropped_events_by_adapter": dropped_events_by_adapter,
            "dropped_events_by_kind": dropped_events_by_kind,
            "coalesced_events_total": coalesced_events_total,
            "adapter_reconnect": reconnect_metrics,
            "active_providers": active_providers,
            "writable_capabilities": writable_capabilities,
            "authority": {
                "semantic_mcp": True,
                "physical_writes": bool(writable_capabilities),
                "battery_dispatch": self.battery_qualification,
            },
            "event_lag_seconds": self.event_consumer.last_event_lag_seconds,
            "event_count": self.event_consumer.events_applied,
            "max_state_age_seconds": max_state_age_seconds,
            "scheduler_lateness_seconds": self.scheduler.last_lateness_seconds,
            "scheduler_max_lateness_seconds": self.scheduler.max_lateness_seconds,
            "scheduler_missed_total": self.scheduler.missed_total,
            "execution_unknown_total": self.scheduler.execution_unknown_total,
            "execution_unavailable_total": self.scheduler.execution_unavailable_total,
            "execution_failed_total": self.scheduler.execution_failed_total,
            "execution_partial_total": self.scheduler.execution_partial_total,
            "stale_state_count": stale_state_count,
            "plans_by_status": plans_by_status,
            "optimizer_last_wall_time_seconds": optimizer_last_wall_time_seconds,
            "optimizer_queue_time_seconds": (
                self.optimization_worker.last_queue_wait_seconds
                if self.optimization_worker is not None
                else None
            ),
            "db_operation_count": db_metrics.operation_count,
            "db_busy_count": db_metrics.busy_count,
            "storage": _storage_metrics_dict(storage_metrics),
            "audit": {
                "storage": _storage_metrics_dict(audit_storage_metrics),
                "sink_failure_count": self.audit.sink_failure_count if self.audit else None,
                "last_sink_error": self.audit.last_sink_error if self.audit else None,
            },
            "operational": operational,
            "household_queue_depth": (
                self.household_work_queues.depths()
                if self.household_work_queues is not None
                else {}
            ),
            "battery_qualification": self.battery_qualification,
        }

    @staticmethod
    def _active_provider_ids(adapter: AdapterPort) -> list[str]:
        children = getattr(adapter, "adapters", ())
        if children:
            return sorted(
                {
                    provider_id
                    for child in children
                    for provider_id in RuntimeMetricsCollector._active_provider_ids(child)
                }
            )
        adapter_id = getattr(adapter, "adapter_id", None)
        return [str(adapter_id)] if adapter_id else []

    def _writable_capabilities(self) -> list[dict[str, Any]]:
        if self.registry is None:
            return []
        writable: list[dict[str, Any]] = []
        for device in self.registry.devices:
            for capability in device.capabilities:
                if not capability.writable:
                    continue
                routes = self.registry.routes_for(device.id, capability.name)
                writable.append(
                    {
                        "device_id": device.id,
                        "capability": capability.name,
                        "commands": list(capability.commands),
                        "route_count": len(routes),
                        "available_route_count": sum(1 for route in routes if route.available),
                    }
                )
        return sorted(writable, key=lambda item: (item["device_id"], item["capability"]))
