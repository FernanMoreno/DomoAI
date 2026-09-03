"""Async SQLite connection and ledger-tracked schema migrations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import quote

try:
    import fcntl
except ImportError:  # pragma: no cover - native Windows uses a different host path.
    fcntl = None  # type: ignore[assignment]

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


@dataclass(frozen=True)
class SQLiteBackupResult:
    """Evidence returned after an online backup finishes."""

    schema_migrations: tuple[str, ...]
    integrity_check: str


class SQLiteAdvisoryLock:
    """Process lock shared by runtime ownership and administrative restore."""

    def __init__(self, database_path: Path) -> None:
        self.path = database_path.with_name(f"{database_path.name}.lock")
        self._handle: BinaryIO | None = None

    def acquire(self, *, blocking: bool = True) -> None:
        if self._handle is not None:
            raise RuntimeError("SQLite advisory lock is already held")
        if fcntl is None:
            raise OSError("SQLite advisory locks require a POSIX host")
        if self.path.is_symlink():
            raise OSError("SQLite advisory lock path cannot be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            # Closing the descriptor still releases the process lock. There
            # is no safe recovery action for an unlock error at shutdown.
            pass
        finally:
            handle.close()


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

    async def open_existing(self) -> None:
        """Open an existing database without creating or migrating it.

        Administrative reads must not turn an empty, corrupt, or partially
        provisioned path into a runtime database. ``mode=rw`` also prevents a
        TOCTOU gap from silently creating a file after the existence check.
        """

        if self.path.is_symlink() or not self.path.is_file():
            raise FileNotFoundError(self.path)
        uri = f"file:{quote(str(self.path.resolve()), safe='/')}?mode=rw"
        self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        self._connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")

    def advisory_lock(self) -> SQLiteAdvisoryLock:
        return SQLiteAdvisoryLock(self.path)

    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError(
                "SQLiteDatabase.initialize() or open_existing() must be called first"
            )
        return cast(sqlite3.Connection, _InstrumentedConnection(self._connection, self._metrics))

    @property
    def metrics(self) -> DbOperationMetrics:
        return replace(self._metrics)

    def backup_to(self, destination: Path) -> SQLiteBackupResult:
        """Create a consistent SQLite copy using the online backup API.

        The caller must invoke this on the database's serialized storage owner.
        In particular, this method intentionally does not copy the main file,
        ``-wal`` file, or ``-shm`` file from a live database.
        """

        connection = self._connection
        if connection is None:
            raise RuntimeError(
                "SQLiteDatabase.initialize() or open_existing() must be called first"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(destination, check_same_thread=False)
        try:
            connection.backup(target)
            target.commit()
            integrity_row = target.execute("PRAGMA integrity_check").fetchone()
            integrity_check = str(integrity_row[0]) if integrity_row else ""
            if integrity_check != "ok":
                raise sqlite3.DatabaseError("backup integrity check failed")
            migrations = tuple(
                str(row[0])
                for row in target.execute(
                    "SELECT filename FROM schema_migrations ORDER BY filename"
                ).fetchall()
            )
            return SQLiteBackupResult(
                schema_migrations=migrations,
                integrity_check=integrity_check,
            )
        finally:
            target.close()

    async def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
