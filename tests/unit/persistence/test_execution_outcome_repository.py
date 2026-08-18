from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.domain.models import ErrorDetail, ExecutionOutcome, ExecutionStatus
from domoai.persistence.repositories import ExecutionOutcomeRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _outcome(*, status: ExecutionStatus, message: str) -> ExecutionOutcome:
    return ExecutionOutcome(
        plan_id="plan-attempts-1",
        command_id="command-attempts-1",
        status=status,
        completed_at=datetime.now(UTC),
        error=ErrorDetail(code="execution_failed", message=message, retryable=True),
    )


@pytest.mark.asyncio
async def test_initialize_creates_both_execution_tables(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()

    tables = {
        row[0]
        for row in database.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert "execution_outcomes" in tables
    assert "execution_attempts" in tables


@pytest.mark.asyncio
async def test_repeated_save_preserves_every_attempt_in_history(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ExecutionOutcomeRepository(database)

    first = _outcome(status=ExecutionStatus.UNAVAILABLE, message="first attempt failed")
    second = _outcome(status=ExecutionStatus.CONFIRMED_SUCCESS, message="second attempt")

    await repository.save(first)
    await repository.save(second)

    attempts = await repository.list_attempts_for_plan("plan-attempts-1")
    assert len(attempts) == 2
    assert attempts[0].status is ExecutionStatus.UNAVAILABLE
    assert attempts[1].status is ExecutionStatus.CONFIRMED_SUCCESS


@pytest.mark.asyncio
async def test_list_for_plan_still_returns_only_the_latest_outcome(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ExecutionOutcomeRepository(database)

    first = _outcome(status=ExecutionStatus.UNAVAILABLE, message="first attempt failed")
    second = _outcome(status=ExecutionStatus.CONFIRMED_SUCCESS, message="second attempt")

    await repository.save(first)
    await repository.save(second)

    latest = await repository.list_for_plan("plan-attempts-1")
    assert len(latest) == 1
    assert latest[0].status is ExecutionStatus.CONFIRMED_SUCCESS
