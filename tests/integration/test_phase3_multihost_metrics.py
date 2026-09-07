from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.mcp.remote_metrics import render_prometheus_metrics
from domoai.persistence.coordination import MetricHistoryRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.instance import InstanceIdentity


@pytest.mark.asyncio
async def test_metric_history_is_bounded_and_secret_safe(tmp_path: Path) -> None:
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    database = SQLiteDatabase(tmp_path / "metrics.sqlite3", clock=FixedClock(now))
    await database.initialize()
    try:
        repository = MetricHistoryRepository(database, clock=FixedClock(now))
        identity = InstanceIdentity(instance_id="host-a", process_start_time=now)
        for value in (1.0, 2.0, 3.0):
            await repository.append(
                instance_id=identity.instance_id,
                process_start_time=identity.process_start_time,
                metric_name="fencing_stale_rejected_total",
                value=value,
                labels={"scope": "home-a"},
                max_samples=2,
            )

        assert await repository.count(instance_id="host-a") == 2
        rendered = render_prometheus_metrics(
            {
                "instance_id": "host-a",
                "process_start_time_seconds": now.timestamp(),
                "operational": {
                    "fencing": {"stale_rejected_total": 3},
                    "bearer": "must-not-export",
                },
            }
        )
        assert 'domoai_instance_info{instance_id="host-a"} 1' in rendered
        assert "must-not-export" not in rendered
    finally:
        await database.close()
