from __future__ import annotations

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.runtime.metrics import RuntimeMetricsCollector


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_metrics_correlate_storage_state_event_and_scheduler_health(tmp_path) -> None:
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "observability.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        metrics = RuntimeMetricsCollector(
            adapter=runtime.adapter,
            event_consumer=runtime.event_consumer,
            scheduler=runtime.scheduler,
            state_store=runtime.state_store,
            plan_repository=runtime.plan_repository,
            database=runtime.database,
            storage=runtime.storage,
            battery_qualification=runtime.battery_qualification,
            clock=runtime.clock,
        )
        snapshot = await metrics.snapshot()

        assert snapshot["max_state_age_seconds"] is not None
        assert snapshot["event_lag_seconds"] is None
        assert snapshot["scheduler_missed_total"] == 0
        assert snapshot["battery_qualification"] == "unsupported"
        assert snapshot["storage"]["operation_count"] > 0
        assert "timeout_count" in snapshot["storage"]
    finally:
        await runtime.close()
