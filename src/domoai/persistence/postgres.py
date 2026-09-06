"""PostgreSQL database boundary for shared multi-host control-plane state."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.conninfo import make_conninfo

from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock

_SCHEMA_PATH = Path(__file__).with_name("postgres_schema.sql")
_POSTGRES_SCHEMA_VERSION = "postgres-schema-001"
_SCHEMA_LOCK_KEY = "domoai:postgres-schema-bootstrap"


class _PostgresConnectionProxy:
    def __init__(self, connection: psycopg.Connection[Any]) -> None:
        self._connection = connection

    def execute(self, sql: str, *args: object) -> Any:
        adapted = PostgresDatabase.adapt_sql(sql)
        if args:
            return self._connection.execute(adapted, cast(Any, args[0]))
        return self._connection.execute(adapted)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class PostgresDatabase(SQLiteDatabase):
    """SQLite-compatible repository database backed by PostgreSQL.

    Existing repositories intentionally depend on a tiny ``connection``
    surface. Keeping that surface lets the shared control plane use the same
    domain serialization and repository behavior while PostgreSQL provides
    cross-process transactions and durability.
    """

    def __init__(
        self,
        dsn: str,
        *,
        clock: Clock | None = None,
        connection_factory: Callable[[str], psycopg.Connection[Any]] | None = None,
    ) -> None:
        super().__init__(Path("postgresql://control-plane"), clock=clock)
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must be non-empty")
        self.dsn = dsn
        self._connection_factory = connection_factory or psycopg.connect
        self._postgres_connection: psycopg.Connection[Any] | None = None
        self._proxy: _PostgresConnectionProxy | None = None

    @staticmethod
    def adapt_sql(sql: str) -> str:
        adapted = re.sub(r"BEGIN\s+IMMEDIATE", "BEGIN", sql, flags=re.IGNORECASE)
        ignored_insert = bool(re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", adapted, re.I))
        adapted = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", adapted, flags=re.I)
        # SQLite uses LIMIT -1 to mean "no limit" when OFFSET is present;
        # PostgreSQL rejects a negative LIMIT, so omit it while preserving the
        # offset used by bounded historical metric cleanup.
        adapted = re.sub(r"LIMIT\s+-1\s+OFFSET", "OFFSET", adapted, flags=re.I)
        adapted = adapted.replace("?", "%s")
        if ignored_insert and "ON CONFLICT" not in adapted.upper():
            adapted = adapted.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return adapted

    @staticmethod
    def schema_sql() -> str:
        return _SCHEMA_PATH.read_text(encoding="utf-8")

    @staticmethod
    def build_dsn(
        dsn: str,
        *,
        sslmode: str,
        sslrootcert: Path,
        sslcert: Path,
        sslkey: Path,
    ) -> str:
        return make_conninfo(
            dsn,
            sslmode=sslmode,
            sslrootcert=str(sslrootcert),
            sslcert=str(sslcert),
            sslkey=str(sslkey),
        )

    async def initialize(self, *, migrations_dir: Path | None = None) -> None:
        connection = self._connection_factory(self.dsn)
        self._postgres_connection = connection
        self._proxy = _PostgresConnectionProxy(connection)
        schema_lock_held = False
        try:
            # Multiple hosts can start against the same Patroni writer at once.
            # PostgreSQL has no useful IF NOT EXISTS equivalent for functions,
            # so serialize the complete schema bootstrap with a session lock.
            connection.execute(
                "SELECT pg_advisory_lock(hashtext(%s))",
                (_SCHEMA_LOCK_KEY,),
            )
            schema_lock_held = True
            connection.execute(self.schema_sql())
            connection.execute(
                """INSERT INTO schema_migrations (filename, applied_at)
                   VALUES (%s, CURRENT_TIMESTAMP)
                   ON CONFLICT(filename) DO NOTHING""",
                (_POSTGRES_SCHEMA_VERSION,),
            )
            connection.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                (_SCHEMA_LOCK_KEY,),
            )
            schema_lock_held = False
            connection.commit()
        except BaseException:
            connection.rollback()
            if schema_lock_held:
                try:
                    connection.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        (_SCHEMA_LOCK_KEY,),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
            connection.close()
            self._postgres_connection = None
            self._proxy = None
            raise

    async def open_existing(self) -> None:
        await self.initialize()

    @property
    def connection(self) -> Any:
        if self._proxy is None:
            raise RuntimeError("PostgresDatabase.initialize() must be called first")
        return self._proxy

    def advisory_lock(self) -> None:  # type: ignore[override]
        """PostgreSQL row transactions replace SQLite's process lock."""

        return None

    def backup_to(self, destination: Path) -> Any:
        raise RuntimeError("use PostgreSQL backup tooling for a PostgreSQL control plane")

    async def close(self) -> None:
        connection = self._postgres_connection
        self._proxy = None
        self._postgres_connection = None
        if connection is not None:
            connection.close()


__all__ = ["PostgresDatabase"]
