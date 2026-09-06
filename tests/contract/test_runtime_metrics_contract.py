from __future__ import annotations

import json

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.metrics import RuntimeMetricsCollector
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings


@pytest.mark.asyncio
async def test_operational_metrics_contract_is_stable_and_json_safe(tmp_path) -> None:
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "metrics-contract.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        snapshot = await RuntimeMetricsCollector(
            adapter=runtime.adapter,
            event_consumer=runtime.event_consumer,
            scheduler=runtime.scheduler,
            state_store=runtime.state_store,
            plan_repository=runtime.plan_repository,
            database=runtime.database,
            storage=runtime.storage,
            audit_storage=runtime.audit_storage,
            audit=runtime.audit,
            operational_metrics=runtime.operational_metrics,
        ).snapshot()

        operational = snapshot["operational"]
        assert set(operational) == {
            "instance_id",
            "process_start_time",
            "process_start_time_seconds",
            "command_latency_ms",
            "command_outcomes",
            "readback_mismatch_total",
            "source_cursor",
            "leases",
            "approvals",
            "bundles",
            "fencing",
            "state_quality_by_source",
            "series_overflow_total",
            "telemetry_failure_total",
        }
        assert set(operational["source_cursor"]) == {
            "gap_total",
            "replay_total",
            "resync_total",
        }
        json.dumps(snapshot, allow_nan=False)
    finally:
        await runtime.close()
