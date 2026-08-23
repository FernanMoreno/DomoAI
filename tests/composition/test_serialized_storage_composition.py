from __future__ import annotations

import asyncio
import sqlite3
import time

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.persistence.serialized import StorageOperationTimeoutError


@pytest.mark.composition
@pytest.mark.asyncio
async def test_sqlite_contention_does_not_block_scheduler_event_loop(tmp_path) -> None:
    database_path = tmp_path / "serialized-runtime.sqlite3"
    runtime = await build_runtime(
        Settings(
            database_path=database_path,
            sqlite_busy_timeout_ms=1000,
            # Allow runtime initialization to complete before tightening the
            # deadline for the contention scenario below.
            sqlite_operation_timeout_seconds=1.0,
            sqlite_worker_queue_capacity=4,
            sqlite_worker_queue_wait_seconds=0.1,
        ),
        adapter=SimulatedHomeAdapter(),
    )
    runtime.storage.operation_timeout_seconds = 0.05
    lock_connection = sqlite3.connect(database_path, timeout=0, check_same_thread=False)
    lock_closed = False
    try:
        lock_connection.execute("BEGIN IMMEDIATE")
        snapshot = (await runtime.state_store.all())[0]
        write_task = asyncio.create_task(runtime.state_snapshot_repository.save(snapshot))

        ticks: list[float] = []
        started = time.monotonic()
        while time.monotonic() - started < 0.12:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

        assert len(ticks) >= 8
        assert write_task.done()
        assert isinstance(write_task.exception(), StorageOperationTimeoutError)

        lock_connection.rollback()
        lock_connection.close()
        lock_closed = True
        # Scheduler repository work resumes once the bounded storage worker
        # has drained the uncertain SQLite operation; the loop itself stayed
        # responsive throughout the lock wait.
        assert await runtime.scheduler.list_pending() == []
        assert runtime.storage.metrics.timeout_count >= 1
    finally:
        if not lock_closed:
            lock_connection.close()
        await runtime.close()
