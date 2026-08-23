from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.persistence.repositories import AuditEventRepository
from domoai.persistence.sqlite import SQLiteDatabase


async def _repository(tmp_path: Path) -> AuditEventRepository:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    return AuditEventRepository(database)


async def _append(
    repository: AuditEventRepository,
    *,
    event_id: str,
    event_type: str = "plan_approved",
    subject_id: str = "plan-1",
    created_at: datetime,
) -> None:
    await repository.append(
        event_id=event_id,
        event_type=event_type,
        actor="system",
        subject_id=subject_id,
        payload={},
        created_at=created_at.isoformat(),
    )


@pytest.mark.asyncio
async def test_list_events_returns_newest_first(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    await _append(repository, event_id="e1", created_at=base)
    await _append(repository, event_id="e2", created_at=base + timedelta(seconds=1))
    await _append(repository, event_id="e3", created_at=base + timedelta(seconds=2))

    events = await repository.list_events()

    assert [event.id for event in events] == ["e3", "e2", "e1"]


@pytest.mark.asyncio
async def test_list_events_defaults_to_at_most_one_hundred(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    for index in range(120):
        await _append(
            repository,
            event_id=f"e{index}",
            created_at=base + timedelta(seconds=index),
        )

    events = await repository.list_events()

    assert len(events) == 100


@pytest.mark.asyncio
async def test_list_events_filters_by_event_type(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    await _append(repository, event_id="e1", event_type="plan_approved", created_at=base)
    await _append(
        repository,
        event_id="e2",
        event_type="precondition_failed",
        created_at=base + timedelta(seconds=1),
    )

    events = await repository.list_events(event_type="precondition_failed")

    assert [event.id for event in events] == ["e2"]


@pytest.mark.asyncio
async def test_list_events_filters_by_subject_id(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    await _append(repository, event_id="e1", subject_id="plan-1", created_at=base)
    await _append(
        repository, event_id="e2", subject_id="plan-2", created_at=base + timedelta(seconds=1)
    )

    events = await repository.list_events(subject_id="plan-2")

    assert [event.id for event in events] == ["e2"]


@pytest.mark.asyncio
async def test_list_events_combines_filters(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    await _append(
        repository,
        event_id="e1",
        event_type="plan_approved",
        subject_id="plan-1",
        created_at=base,
    )
    await _append(
        repository,
        event_id="e2",
        event_type="plan_approved",
        subject_id="plan-2",
        created_at=base + timedelta(seconds=1),
    )
    await _append(
        repository,
        event_id="e3",
        event_type="precondition_failed",
        subject_id="plan-1",
        created_at=base + timedelta(seconds=2),
    )

    events = await repository.list_events(event_type="plan_approved", subject_id="plan-1")

    assert [event.id for event in events] == ["e1"]


@pytest.mark.asyncio
async def test_list_events_since_excludes_at_or_before(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    await _append(repository, event_id="e1", created_at=base)
    cutoff = base + timedelta(seconds=1)
    await _append(repository, event_id="e2", created_at=cutoff)
    await _append(repository, event_id="e3", created_at=base + timedelta(seconds=2))

    events = await repository.list_events(since=cutoff)

    assert [event.id for event in events] == ["e3"]


@pytest.mark.asyncio
async def test_list_events_since_in_the_future_returns_empty(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    await _append(repository, event_id="e1", created_at=datetime.now(UTC))

    events = await repository.list_events(since=datetime.now(UTC) + timedelta(days=1))

    assert events == []


@pytest.mark.asyncio
async def test_list_events_since_compares_absolute_instants_across_offsets(
    tmp_path: Path,
) -> None:
    repository = await _repository(tmp_path)
    await repository.append(
        event_id="before",
        event_type="test",
        actor="system",
        subject_id="plan-1",
        payload={},
        created_at="2026-08-21T10:00:00+02:00",  # 08:00 UTC
    )
    await repository.append(
        event_id="after",
        event_type="test",
        actor="system",
        subject_id="plan-1",
        payload={},
        created_at="2026-08-21T09:30:00+00:00",
    )

    events = await repository.list_events(since=datetime(2026, 8, 21, 9, 0, tzinfo=UTC))

    assert [event.id for event in events] == ["after"]


@pytest.mark.asyncio
async def test_list_events_rejects_naive_since(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        await repository.list_events(since=datetime(2026, 8, 21, 9, 0))


@pytest.mark.asyncio
async def test_list_events_caps_at_hard_maximum(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    for index in range(520):
        await _append(
            repository,
            event_id=f"e{index}",
            created_at=base + timedelta(seconds=index),
        )

    events = await repository.list_events(limit=100_000)

    assert len(events) == 500


@pytest.mark.asyncio
async def test_list_events_honors_a_small_requested_limit(tmp_path: Path) -> None:
    repository = await _repository(tmp_path)
    base = datetime.now(UTC)
    for index in range(10):
        await _append(
            repository,
            event_id=f"e{index}",
            created_at=base + timedelta(seconds=index),
        )

    events = await repository.list_events(limit=5)

    assert len(events) == 5
