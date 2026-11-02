from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from testcontainers.community.postgres import PostgresContainer

from domoai.persistence.postgres import PostgresDatabase

pytestmark = pytest.mark.composition


@pytest.fixture(scope="module")
def postgres_server() -> Iterator[str]:
    import docker

    try:
        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker is unavailable")
    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        yield container.get_connection_url(driver=None)


@pytest.fixture
def new_database(postgres_server: str) -> Iterator[Callable[[], str]]:
    names: list[str] = []
    with psycopg.connect(postgres_server, autocommit=True) as admin:

        def create() -> str:
            name = f"bootstrap_{uuid4().hex}"
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            names.append(name)
            return make_conninfo(postgres_server, dbname=name)

        yield create
        for name in names:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _paused_connection_type(
    before_commit: Queue[int], release_commit: Event
) -> type[psycopg.Connection[Any]]:
    class PausedCommitConnection(psycopg.Connection[Any]):
        # Both psycopg commit() and transaction() send COMMIT here. Only the
        # scheduling is intercepted: all SQL and locks run on real PostgreSQL.
        def _exec_command(self, command: Any, result_format: Any = psycopg.pq.Format.TEXT) -> Any:
            if command == b"COMMIT":
                before_commit.put(self.info.backend_pid)
                if not release_commit.wait(20):
                    raise TimeoutError("bootstrap commit was not released")
            return (yield from super()._exec_command(command, result_format))

    return PausedCommitConnection


def _wait_event(observer: psycopg.Connection[Any], pid: int) -> str:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = observer.execute(
            "SELECT wait_event_type, wait_event FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if row is not None and row[0] == "Lock":
            return str(row[1])
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} did not reach a PostgreSQL lock wait")


@pytest.mark.parametrize("autocommit", [False, True])
def test_concurrent_bootstrap_holds_ownership_until_schema_is_committed(
    new_database: Callable[[], str], autocommit: bool
) -> None:
    dsn = new_database()
    start = Barrier(3)
    before_commit: Queue[int] = Queue()
    release_commit = Event()
    connection_type = _paused_connection_type(before_commit, release_commit)
    connections: list[psycopg.Connection[Any]] = []

    def connect(value: str) -> psycopg.Connection[Any]:
        connection = connection_type.connect(value, autocommit=autocommit)
        connections.append(connection)
        start.wait(timeout=20)
        return connection

    databases = [PostgresDatabase(dsn, connection_factory=connect) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(asyncio.run, database.initialize()) for database in databases]
        try:
            start.wait(timeout=20)
            winner_pid = before_commit.get(timeout=20)
            follower = next(c for c in connections if c.info.backend_pid != winner_pid)
            with psycopg.connect(dsn, autocommit=True) as observer:
                waiting_on = _wait_event(observer, follower.info.backend_pid)
            for database in databases:
                with pytest.raises(RuntimeError, match="initialize"):
                    _ = database.connection
            release_commit.set()
            # The original code reaches a catalog UniqueViolation here because
            # the follower entered CREATE FUNCTION before the winner committed.
            for future in futures:
                future.result(timeout=20)
            assert waiting_on == "advisory"
            databases[0].connection.execute(
                "INSERT INTO devices (id, payload, updated_at) VALUES (?, ?, ?)",
                ("winner-device", "{}", "2026-09-10"),
            )
            databases[0].connection.commit()
            assert databases[1].connection.execute("SELECT id FROM devices").fetchone() == (
                "winner-device",
            )
            databases[1].connection.execute(
                "INSERT INTO devices (id, payload, updated_at) VALUES (?, ?, ?)",
                ("follower-device", "{}", "2026-09-10"),
            )
            databases[1].connection.commit()
            assert databases[0].connection.execute("SELECT count(*) FROM devices").fetchone() == (
                2,
            )
        finally:
            release_commit.set()
            for future in futures:
                try:
                    future.result(timeout=20)
                except Exception:
                    pass
            for database in databases:
                asyncio.run(database.close())


@pytest.mark.parametrize("autocommit", [False, True])
async def test_bootstrap_restart_preserves_rows_and_migration_record(
    new_database: Callable[[], str], autocommit: bool
) -> None:
    dsn = new_database()
    database = PostgresDatabase(
        dsn, connection_factory=lambda value: psycopg.connect(value, autocommit=autocommit)
    )
    await database.initialize()
    database.connection.execute(
        "INSERT INTO devices (id, payload, updated_at) VALUES (?, ?, ?)",
        ("preserved", "{}", "2026-09-10"),
    )
    database.connection.commit()
    await database.close()
    await database.open_existing()
    try:
        assert database.connection.execute("SELECT id FROM devices").fetchall() == [("preserved",)]
        assert database.connection.execute("SELECT count(*) FROM schema_migrations").fetchone() == (
            1,
        )
    finally:
        await database.close()


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize("failure_at_commit", [False, True])
async def test_failed_bootstrap_rolls_back_schema_and_releases_ownership(
    new_database: Callable[[], str],
    autocommit: bool,
    failure_at_commit: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = new_database()
    connection = psycopg.connect(dsn, autocommit=autocommit)
    database = PostgresDatabase(dsn, connection_factory=lambda _: connection)
    schema = PostgresDatabase.schema_sql()
    # Fail after DDL, either at the marker INSERT or at the actual COMMIT.
    if failure_at_commit:
        rejection = (
            "CREATE TABLE allowed_migrations (filename TEXT PRIMARY KEY); "
            "ALTER TABLE schema_migrations ADD CONSTRAINT reject_bootstrap "
            "FOREIGN KEY (filename) REFERENCES allowed_migrations(filename) "
            "DEFERRABLE INITIALLY DEFERRED;"
        )
        expected_error = psycopg.errors.ForeignKeyViolation
    else:
        rejection = (
            "ALTER TABLE schema_migrations ADD CONSTRAINT reject_bootstrap "
            "CHECK (filename = 'rejected');"
        )
        expected_error = psycopg.errors.CheckViolation
    with monkeypatch.context() as patch:
        patch.setattr(
            PostgresDatabase,
            "schema_sql",
            staticmethod(lambda: schema + "\n" + rejection),
        )
        with pytest.raises(expected_error):
            await database.initialize()
    assert connection.closed
    with pytest.raises(RuntimeError, match="initialize"):
        _ = database.connection
    with psycopg.connect(dsn, autocommit=True) as observer:
        assert observer.execute("SELECT to_regclass('devices')").fetchone() == (None,)
        assert observer.execute("SELECT to_regclass('schema_migrations')").fetchone() == (None,)
        assert observer.execute("SELECT to_regprocedure('json_extract(text,text)')").fetchone() == (
            None,
        )
    retry = PostgresDatabase(
        dsn,
        connection_factory=lambda value: psycopg.connect(value, options="-c lock_timeout=2000"),
    )
    await retry.initialize()
    try:
        assert retry.connection.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)
    finally:
        await retry.close()


def test_bootstrap_ownership_is_scoped_to_one_database(
    new_database: Callable[[], str],
) -> None:
    before_commit: Queue[int] = Queue()
    release_commit = Event()
    connection_type = _paused_connection_type(before_commit, release_commit)
    first = PostgresDatabase(new_database(), connection_factory=connection_type.connect)
    second = PostgresDatabase(new_database())
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(asyncio.run, first.initialize())
        try:
            before_commit.get(timeout=20)
            second_future = pool.submit(asyncio.run, second.initialize())
            second_future.result(timeout=10)
            assert second.connection.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone() == (1,)
        finally:
            release_commit.set()
            first_future.result(timeout=20)
            asyncio.run(first.close())
            asyncio.run(second.close())


async def test_bootstrap_commits_factory_connection_setup_before_returning(
    new_database: Callable[[], str],
) -> None:
    dsn = new_database()
    connection = psycopg.connect(dsn)
    connection.execute("SET search_path TO public")
    database = PostgresDatabase(dsn, connection_factory=lambda _: connection)
    await database.initialize()
    try:
        assert connection.info.transaction_status is psycopg.pq.TransactionStatus.IDLE
        with psycopg.connect(dsn, autocommit=True) as observer:
            assert observer.execute("SELECT count(*) FROM schema_migrations").fetchone() == (1,)
            assert observer.execute(
                "SELECT count(*) FROM pg_locks WHERE pid = %s AND locktype = 'advisory'",
                (connection.info.backend_pid,),
            ).fetchone() == (0,)
    finally:
        await database.close()
