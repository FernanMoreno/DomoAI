from __future__ import annotations

from domoai.persistence.postgres import PostgresDatabase


class _RecordingPostgresConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.committed = False
        self.closed = False

    def execute(self, sql: str, *args: object) -> None:
        del args
        self.statements.append(sql)

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
    assert PostgresDatabase.adapt_sql(
        "INSERT OR IGNORE INTO audit_events (id) VALUES (?)"
    ) == "INSERT INTO audit_events (id) VALUES (%s) ON CONFLICT DO NOTHING"
    assert PostgresDatabase.adapt_sql(
        "SELECT * FROM operational_metric_history LIMIT -1 OFFSET ?"
    ) == "SELECT * FROM operational_metric_history OFFSET %s"


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

    assert "pg_advisory_lock" in connection.statements[0]
    assert "CREATE OR REPLACE FUNCTION json_extract" in connection.statements[1]
    assert any("pg_advisory_unlock" in statement for statement in connection.statements)
    assert connection.committed is True
