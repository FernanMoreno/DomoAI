from datetime import UTC, datetime

import pytest

from domoai.domain.models import AuditEvent
from domoai.persistence.audit_outbox import AuditOutboxDispatcher, AuditOutboxDispatchResult
from domoai.persistence.repositories import AuditEventRepository
from domoai.persistence.sqlite import SQLiteDatabase


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
