"""Bounded household queue load generator for the disposable lab."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from domoai.application.household_queue import HouseholdWorkQueues, QueueOverloaded
from domoai.persistence.coordination import MetricHistoryRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import SystemClock


async def _metric_history_is_bounded() -> bool:
    """Exercise the same bounded metric repository used by the host agent."""

    with tempfile.TemporaryDirectory(prefix="domoai-lab-load-") as directory:
        database = SQLiteDatabase(Path(directory) / "metrics.sqlite3")
        await database.initialize()
        try:
            repository = MetricHistoryRepository(database)
            process_start_time = SystemClock().now()
            for sample in range(12):
                await repository.append(
                    instance_id="lab-load-generator",
                    process_start_time=process_start_time,
                    metric_name="lab_queue_depth",
                    value=float(sample),
                    max_samples=8,
                )
            return await repository.count(instance_id="lab-load-generator") <= 8
        finally:
            await database.close()


async def _run(max_per_household: int, max_total: int, requests: int) -> dict[str, object]:
    queues = HouseholdWorkQueues(max_per_household=max_per_household, max_total=max_total)
    release = asyncio.Event()
    rejected = 0
    maximum = 0

    async def work() -> None:
        await release.wait()

    async def submit() -> None:
        nonlocal rejected, maximum
        try:
            task = asyncio.create_task(queues.submit("lab-household", work))
            await asyncio.sleep(0)
            maximum = max(maximum, sum(queues.depths().values()))
            await task
        except QueueOverloaded:
            rejected += 1

    tasks = [asyncio.create_task(submit()) for _ in range(requests)]
    await asyncio.sleep(0.05)
    maximum = max(maximum, sum(queues.depths().values()))
    release.set()
    await asyncio.gather(*tasks)
    await queues.close()
    metric_history_bounded = await _metric_history_is_bounded()
    return {
        "status": "passed"
        if maximum <= max_total and rejected > 0 and metric_history_bounded
        else "failed",
        "max_queue_depth": maximum,
        "rejected_requests": rejected,
        "metric_history_bounded": metric_history_bounded,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-per-household", type=int, required=True)
    parser.add_argument("--max-total", type=int, required=True)
    parser.add_argument("--requests", type=int, required=True)
    args = parser.parse_args()
    result = asyncio.run(_run(args.max_per_household, args.max_total, args.requests))
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
