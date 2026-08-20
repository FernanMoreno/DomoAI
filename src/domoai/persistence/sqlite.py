"""Async SQLite connection and ledger-tracked schema migrations."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_BOOTSTRAP_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


class SQLiteDatabase:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None

    async def initialize(self, *, migrations_dir: Path | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        self._connection.executescript(_BOOTSTRAP_LEDGER)
        self._connection.commit()
        applied = {
            row[0]
            for row in self._connection.execute("SELECT filename FROM schema_migrations")
        }
        for migration in sorted((migrations_dir or MIGRATIONS_DIR).glob("*.sql")):
            if migration.name in applied:
                continue
            try:
                self._connection.executescript(migration.read_text(encoding="utf-8"))
            except sqlite3.OperationalError as error:
                if "duplicate column name" not in str(error):
                    raise
                # ALTER TABLE ... ADD COLUMN has no native "IF NOT EXISTS" form
                # in SQLite; a duplicate-column failure here means this
                # migration's effect is already present (e.g. the schema was
                # reconstructed from a snapshot after the ledger lost track of
                # which migrations had run) — equivalent to the CREATE TABLE
                # IF NOT EXISTS idempotency every other migration already
                # relies on, not a real failure.
                self._connection.rollback()
            self._connection.execute(
                "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, ?)",
                (migration.name, datetime.now(UTC).isoformat()),
            )
            self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLiteDatabase.initialize() must be called first")
        return self._connection

    async def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
