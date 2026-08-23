"""Async SQLite connection and ledger-tracked schema migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from domoai.runtime.clock import Clock, SystemClock

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


@dataclass
class DbOperationMetrics:
    operation_count: int = 0
    busy_count: int = 0


class _InstrumentedConnection:
    """Thin proxy counting `execute()` calls and busy/locked errors.

    Transparently forwards everything else to the wrapped connection so
    every existing `database.connection.execute(...)`/`.commit()` call site
    is instrumented without any repository-level change (Spec 079).
    """

    def __init__(self, real: Any, metrics: DbOperationMetrics) -> None:
        self._real = real
        self._metrics = metrics

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        self._metrics.operation_count += 1
        try:
            return cast(sqlite3.Cursor, self._real.execute(sql, *args))
        except sqlite3.OperationalError as error:
            if "database is locked" in str(error):
                self._metrics.busy_count += 1
            raise

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


class SQLiteDatabase:
    def __init__(
        self, path: Path, *, busy_timeout_ms: int = 5000, clock: Clock | None = None
    ) -> None:
        self.path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._clock = clock or SystemClock()
        self._connection: sqlite3.Connection | None = None
        self._metrics = DbOperationMetrics()

    async def initialize(self, *, migrations_dir: Path | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Runtime repository calls are serialized on a dedicated storage
        # thread. ``check_same_thread=False`` is intentional: the connection
        # is still single-owner at runtime, while startup/diagnostic close and
        # tests may inspect it from their event-loop thread.
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        self._connection.executescript(_BOOTSTRAP_LEDGER)
        self._connection.commit()
        applied = {
            row[0] for row in self._connection.execute("SELECT filename FROM schema_migrations")
        }
        # sqlite3's default (legacy) transaction control implicitly commits
        # DDL/DML executed via executescript() regardless of any surrounding
        # transaction, so a crash between a migration's SQL and its ledger
        # entry could durably apply the migration with no ledger record —
        # for a script combining a non-idempotent step (e.g. ALTER TABLE)
        # with a following step (e.g. an UPDATE backfill), a crash between
        # those two statements silently strands the backfill forever, since
        # a retry's ALTER alone then looks like the harmless "already
        # applied" case below. Manual transaction control makes the whole
        # script and its ledger entry commit or roll back together.
        self._connection.autocommit = False
        try:
            for migration in sorted((migrations_dir or MIGRATIONS_DIR).glob("*.sql")):
                if migration.name in applied:
                    continue
                migration_sql = migration.read_text(encoding="utf-8")
                try:
                    self._connection.executescript(migration_sql)
                except sqlite3.OperationalError as error:
                    self._connection.rollback()
                    if "duplicate column name" not in str(error):
                        raise
                    # ALTER TABLE ... ADD COLUMN has no native "IF NOT EXISTS"
                    # form in SQLite; a duplicate-column failure here means
                    # this migration's structural step is already present
                    # (e.g. the schema was reconstructed from a snapshot after
                    # the ledger lost track of which migrations had run).
                    # Re-run the remaining statements instead of merely
                    # registering the migration: a following backfill must not
                    # be lost in this historical-recovery path.
                    for statement in migration_sql.split(";"):
                        normalized = statement.strip()
                        if not normalized:
                            continue
                        if normalized.upper().startswith("ALTER TABLE") and " ADD COLUMN " in (
                            normalized.upper()
                        ):
                            continue
                        self._connection.execute(normalized)
                self._connection.execute(
                    "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                    (migration.name, self._clock.now().isoformat()),
                )
                self._connection.commit()
        finally:
            self._connection.autocommit = sqlite3.LEGACY_TRANSACTION_CONTROL
            # Leave repository callers at a clean transaction boundary. In
            # Python 3.12 restoring legacy transaction control can otherwise
            # leave the connection inside the last migration transaction,
            # preventing an explicit BEGIN IMMEDIATE from acquiring the
            # lifecycle write lock.
            self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteDatabase.initialize() must be called first")
        return cast(sqlite3.Connection, _InstrumentedConnection(self._connection, self._metrics))

    @property
    def metrics(self) -> DbOperationMetrics:
        return replace(self._metrics)

    async def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
