from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from domoai.persistence.postgres import PostgresDatabase


class _RecordingPostgresConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = False
        self.closed = False

    def execute(self, sql: str, *args: object):
        del args
        self.statements.append(sql)
        return self

    def fetchone(self):
        return (None,)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield
        self.commit()

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.committed = False

    def close(self) -> None:
        self.closed = True


def test_postgres_sql_adapter_converts_sqlite_repository_statements() -> None:
    assert PostgresDatabase.adapt_sql("BEGIN IMMEDIATE") == "BEGIN"
    assert PostgresDatabase.adapt_sql("SELECT * FROM plans WHERE id = ?") == (
        "SELECT * FROM plans WHERE id = %s"
    )
    assert (
        PostgresDatabase.adapt_sql("INSERT OR IGNORE INTO audit_events (id) VALUES (?)")
        == "INSERT INTO audit_events (id) VALUES (%s) ON CONFLICT DO NOTHING"
    )
    assert (
        PostgresDatabase.adapt_sql("SELECT * FROM operational_metric_history LIMIT -1 OFFSET ?")
        == "SELECT * FROM operational_metric_history OFFSET %s"
    )


def test_postgres_schema_declares_shared_phase3_tables() -> None:
    schema = PostgresDatabase.schema_sql()

    for table in (
        "plans",
        "approval_grants",
        "execution_outcomes",
        "audit_outbox",
        "physical_intents",
        "operational_metric_history",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema
    assert "'dispatching'" in schema
    assert "owner_id TEXT" in schema
    assert "execution_attempt_id TEXT" in schema


def test_postgres_schema_is_not_sqlite_only() -> None:
    schema = PostgresDatabase.schema_sql()

    assert "BIGSERIAL" in schema
    assert "CREATE OR REPLACE FUNCTION json_extract" in schema
    assert "AUTOINCREMENT" not in schema


async def test_postgres_initialization_serializes_shared_schema_bootstrap() -> None:
    connection = _RecordingPostgresConnection()
    database = PostgresDatabase(
        "postgresql://test",
        connection_factory=lambda _: connection,  # type: ignore[arg-type]
    )

    await database.initialize()

    def position(fragment: str) -> int:
        return next(i for i, statement in enumerate(connection.statements) if fragment in statement)

    lock = position("pg_advisory_xact_lock")
    quarantine = position("to_regnamespace('domoai_restore_guard')")
    floor = position("to_regclass('public.runtime_contract_floor')")
    schema = position("CREATE OR REPLACE FUNCTION json_extract")
    assert lock < quarantine < schema
    assert lock < floor < schema
    assert connection.committed is True
