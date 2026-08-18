from __future__ import annotations

import sqlite3
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


class _FailingExecutionAttemptsConnection:
    """Delegates to a real connection, but fails the execution_attempts insert."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if "INSERT INTO execution_attempts" in sql:
            raise sqlite3.IntegrityError("simulated execution_attempts failure")
        return self._real.execute(sql, *args)

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def _fail_execution_attempts_insert(database: SQLiteDatabase) -> sqlite3.Connection:
    real_connection = database.connection
    database._connection = _FailingExecutionAttemptsConnection(real_connection)  # type: ignore[assignment]
    return real_connection


@pytest.mark.asyncio
async def test_save_rolls_back_the_first_insert_when_the_second_fails(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ExecutionOutcomeRepository(database)
    outcome = _outcome(status=ExecutionStatus.CONFIRMED_SUCCESS, message="will fail")

    real_connection = _fail_execution_attempts_insert(database)
    with pytest.raises(sqlite3.IntegrityError):
        await repository.save(outcome)
    database._connection = real_connection  # type: ignore[assignment]

    outcomes = await repository.list_for_plan("plan-attempts-1")
    attempts = await repository.list_attempts_for_plan("plan-attempts-1")
    assert outcomes == []
    assert attempts == []


@pytest.mark.asyncio
async def test_save_failure_does_not_swallow_or_retype_the_exception(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ExecutionOutcomeRepository(database)
    outcome = _outcome(status=ExecutionStatus.CONFIRMED_SUCCESS, message="will fail")

    _fail_execution_attempts_insert(database)
    with pytest.raises(sqlite3.IntegrityError, match="simulated execution_attempts failure"):
        await repository.save(outcome)


@pytest.mark.asyncio
async def test_connection_remains_usable_after_a_rolled_back_save(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ExecutionOutcomeRepository(database)
    failing_outcome = _outcome(status=ExecutionStatus.CONFIRMED_SUCCESS, message="will fail")

    real_connection = _fail_execution_attempts_insert(database)
    with pytest.raises(sqlite3.IntegrityError):
        await repository.save(failing_outcome)
    database._connection = real_connection  # type: ignore[assignment]

    other_outcome = ExecutionOutcome(
        plan_id="plan-attempts-2",
        command_id="command-attempts-2",
        status=ExecutionStatus.CONFIRMED_SUCCESS,
        completed_at=datetime.now(UTC),
    )
    await repository.save(other_outcome)

    outcomes = await repository.list_for_plan("plan-attempts-2")
    attempts = await repository.list_attempts_for_plan("plan-attempts-2")
    assert len(outcomes) == 1
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_successful_save_still_inserts_both_rows(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = ExecutionOutcomeRepository(database)
    outcome = _outcome(status=ExecutionStatus.CONFIRMED_SUCCESS, message="ok")

    await repository.save(outcome)

    outcomes = await repository.list_for_plan("plan-attempts-1")
    attempts = await repository.list_attempts_for_plan("plan-attempts-1")
    assert len(outcomes) == 1
    assert len(attempts) == 1
