"""Audit congestion must never block the authoritative storage lane.

Closes P1.6 from the 2026-08-24 re-audit of commit 61439f3: before this fix,
`AuditEventRepository` shared the same `SerializedStorageExecutor` (one
admission queue, one bounded semaphore, one worker thread) as
plan/outcome/schedule/bundle writes. Audit is high-volume, fire-and-forget
traffic; a burst of it could exhaust the shared queue's admission slots and
cause an *authoritative* write to time out or be rejected -- and
`AuditLog.append` had no guard against its own sink raising, so a saturated
audit sink could raise straight into whatever lifecycle code called
`audit.append()`.

`domoai.application.runtime_factory.build_runtime` now wires two separate
`SerializedStorageExecutor` instances (`storage` for authoritative
repositories, `audit_storage` for the audit sink only), and `AuditLog.append`
swallows a failing sink instead of propagating it. This test exercises the
real threading/queue/semaphore machinery of both lanes (not a mock of
`SerializedStorageExecutor` itself) to prove the isolation actually holds.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import AuditEvent
from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
from domoai.runtime.events import AuditLog


class _BlockingSink:
    """A synchronous audit sink that blocks until released, like a stalled
    SQLite write competing with real contention on the same file."""

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self.attempts = 0

    def append_event(self, event: AuditEvent) -> None:
        self.attempts += 1
        self._release.wait(timeout=5)


@pytest.mark.composition
@pytest.mark.asyncio
async def test_audit_lane_congestion_does_not_block_authoritative_lane() -> None:
    # capacity=1 -> max_in_flight = 2: the third concurrent submission must
    # be rejected by the audit lane's own admission control.
    audit_storage = SerializedStorageExecutor(
        queue_capacity=1, queue_wait_seconds=0.05, operation_timeout_seconds=5.0
    )
    authoritative_storage = SerializedStorageExecutor(
        queue_capacity=8, queue_wait_seconds=1.0, operation_timeout_seconds=5.0
    )
    release = threading.Event()
    sink = _BlockingSink(release)
    wrapped_sink = SerializedRepositoryProxy(sink, audit_storage)
    audit = AuditLog(sink=wrapped_sink)  # type: ignore[arg-type]

    try:
        # Saturate the audit lane. AuditLog must swallow every overload, not
        # raise into the caller -- exactly as it would if this were
        # `executor.py` calling `audit.append()` mid plan-execution.
        for index in range(6):
            event = audit.append(
                event_type="noise", actor="test", subject_id=str(index), payload={}
            )
            assert event in audit.events  # always kept in-memory regardless of sink outcome

        assert audit.sink_failure_count >= 1
        assert "StorageOverloadedError" in (audit.last_sink_error or "")

        # The authoritative lane is a separate executor with its own
        # queue/semaphore/thread: it must complete quickly and successfully
        # while the audit lane above is still fully occupied by blocked
        # sink calls waiting on `release`.
        started = time.monotonic()
        result: Any = await authoritative_storage.run(lambda: "settled")
        elapsed = time.monotonic() - started

        assert result == "settled"
        assert elapsed < 1.0
        assert authoritative_storage.metrics.overloaded_count == 0
        assert authoritative_storage.metrics.timeout_count == 0
    finally:
        release.set()
        await audit_storage.close()
        await authoritative_storage.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_storage_lanes_own_distinct_sqlite_connections(tmp_path) -> None:
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "runtime.sqlite3"),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        assert runtime.database is not runtime.audit_database
        primary = runtime.database.connection._real  # type: ignore[attr-defined]
        audit = runtime.audit_database.connection._real  # type: ignore[attr-defined]
        assert primary is not audit
    finally:
        await runtime.close()
