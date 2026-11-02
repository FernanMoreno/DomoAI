import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import AuditEvent
from domoai.persistence.audit_outbox import AuditOutboxDispatcher, AuditOutboxDispatchResult
from domoai.persistence.repositories import AuditEventRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock


def _critical_event(event_id: str = "audit-critical-test") -> AuditEvent:
    return AuditEvent(
        id=event_id,
        event_type="plan_execution_completed",
        actor="runtime",
        subject_id="plan-test",
        payload={"token": "must-not-persist"},
        created_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_critical_audit_event_is_persisted_in_ordered_outbox(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "audit.sqlite3")
    await database.initialize()
    repository = AuditEventRepository(database)
    event = AuditEvent(
        id="audit-critical-1",
        event_type="plan_execution_started",
        actor="runtime",
        subject_id="plan-1",
        payload={"token": "must-not-persist"},
        created_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )

    repository.append_event(event)
    repository.append_event(event)

    row = database.connection.execute(
        "SELECT sequence, event_id, status, attempts FROM audit_outbox WHERE event_id = ?",
        (event.id,),
    ).fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1:] == (event.id, "pending", 0)
    persisted = await repository.list_all()
    assert len(persisted) == 1
    assert persisted[0].payload["token"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_outbox_dispatcher_retries_without_duplicate_delivery(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "audit-dispatch.sqlite3")
    await database.initialize()
    repository = AuditEventRepository(database)
    event = AuditEvent(
        id="audit-critical-dispatch-1",
        event_type="plan_execution_completed",
        actor="runtime",
        subject_id="plan-dispatch-1",
        payload={},
        created_at=datetime(2026, 9, 4, 12, tzinfo=UTC),
    )
    repository.append_event(event)
    attempts = 0
    delivered: list[str] = []

    async def flaky_delivery(candidate: AuditEvent) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("downstream unavailable")
        delivered.append(candidate.id)

    dispatcher = AuditOutboxDispatcher(repository, flaky_delivery)
    assert await dispatcher.dispatch_once() == AuditOutboxDispatchResult(delivered=0, retried=1)
    assert await dispatcher.dispatch_once() == AuditOutboxDispatchResult(delivered=1, retried=0)
    assert delivered == [event.id]
    row = database.connection.execute(
        "SELECT status, attempts FROM audit_outbox WHERE event_id = ?", (event.id,)
    ).fetchone()
    assert row == ("delivered", 1)


@pytest.mark.asyncio
async def test_two_dispatchers_cannot_deliver_the_same_claim_concurrently(tmp_path) -> None:
    database_path = tmp_path / "audit-dispatch-race.sqlite3"
    first_database = SQLiteDatabase(database_path)
    second_database = SQLiteDatabase(database_path)
    await first_database.initialize()
    await second_database.initialize()
    first_repository = AuditEventRepository(first_database)
    second_repository = AuditEventRepository(second_database)
    event = _critical_event("audit-critical-race-1")
    first_repository.append_event(event)
    started = asyncio.Event()
    release = asyncio.Event()
    active_callbacks = 0

    async def blocking_delivery(candidate: AuditEvent) -> None:
        nonlocal active_callbacks
        assert candidate.id == event.id
        active_callbacks += 1
        started.set()
        await release.wait()
        active_callbacks -= 1

    async def unexpected_second_delivery(candidate: AuditEvent) -> None:
        raise AssertionError(f"duplicate callback for {candidate.id}")

    first = AuditOutboxDispatcher(
        first_repository,
        blocking_delivery,
        owner_id="worker-a",
        lease_seconds=30,
    )
    second = AuditOutboxDispatcher(
        second_repository,
        unexpected_second_delivery,
        owner_id="worker-b",
        lease_seconds=30,
    )
    first_task = asyncio.create_task(first.dispatch_once())
    await started.wait()
    assert active_callbacks == 1
    assert await second.dispatch_once() == AuditOutboxDispatchResult()
    release.set()
    assert await first_task == AuditOutboxDispatchResult(delivered=1)
    assert active_callbacks == 0
    try:
        assert (
            first_database.connection.execute(
                "SELECT status FROM audit_outbox WHERE event_id = ?", (event.id,)
            ).fetchone()
            == ("delivered",)
        )
    finally:
        await first_database.close()
        await second_database.close()


@pytest.mark.asyncio
async def test_expired_outbox_lease_is_reclaimed_and_stale_worker_cannot_settle(tmp_path) -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    clock = FixedClock(now)
    database = SQLiteDatabase(tmp_path / "audit-dispatch-lease.sqlite3", clock=clock)
    await database.initialize()
    repository = AuditEventRepository(database)
    event = _critical_event("audit-critical-lease-1")
    repository.append_event(event)

    first_claim = await repository.claim_pending_outbox(
        limit=1,
        owner_id="worker-a",
        lease_id="lease-a",
        lease_seconds=5,
        now=clock.now(),
    )
    assert [candidate.id for candidate in first_claim] == [event.id]
    assert await repository.claim_pending_outbox(
        limit=1,
        owner_id="worker-b",
        lease_id="lease-b",
        lease_seconds=5,
        now=clock.now(),
    ) == []

    clock.set(now + timedelta(seconds=6))
    second_claim = await repository.claim_pending_outbox(
        limit=1,
        owner_id="worker-b",
        lease_id="lease-b",
        lease_seconds=5,
        now=clock.now(),
    )
    assert [candidate.id for candidate in second_claim] == [event.id]
    assert not await repository.mark_outbox_delivered(
        event.id, owner_id="worker-a", lease_id="lease-a"
    )
    assert not await repository.record_outbox_retry(
        event.id, "stale", owner_id="worker-a", lease_id="lease-a"
    )
    assert await repository.mark_outbox_delivered(
        event.id, owner_id="worker-b", lease_id="lease-b"
    )
    await database.close()


@pytest.mark.asyncio
async def test_delivery_before_ack_retries_same_event_id(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "audit-dispatch-idempotency.sqlite3")
    await database.initialize()
    repository = AuditEventRepository(database)
    event = _critical_event("audit-critical-idempotency-1")
    repository.append_event(event)
    delivered_ids: list[str] = []

    async def delivery_before_ack(candidate: AuditEvent) -> None:
        delivered_ids.append(candidate.id)
        if len(delivered_ids) == 1:
            raise RuntimeError("ack lost after destination accepted event")

    dispatcher = AuditOutboxDispatcher(
        repository, delivery_before_ack, owner_id="worker-idempotent", lease_seconds=1
    )
    assert await dispatcher.dispatch_once() == AuditOutboxDispatchResult(retried=1)
    assert await dispatcher.dispatch_once() == AuditOutboxDispatchResult(delivered=1)
    assert delivered_ids == [event.id, event.id]
    await database.close()
