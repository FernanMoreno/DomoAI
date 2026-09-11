from __future__ import annotations

from pathlib import Path

import pytest

from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.postgres_migration import migrate_sqlite_to_postgres
from domoai.persistence.sqlite import SQLiteDatabase
from tests.unit.persistence.test_connection_repository import _profile


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_sqlite_to_postgres_migration_reports_counts_and_digests(tmp_path: Path) -> None:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        source = SQLiteDatabase(tmp_path / "source.sqlite3")
        destination = PostgresDatabase(container.get_connection_url(driver=None))
        await source.initialize()
        await destination.initialize()
        try:
            source.connection.execute(
                "INSERT INTO devices (id, payload, updated_at) VALUES (?, ?, ?)",
                ("device-1", '{"id":"device-1"}', "2026-09-05T00:00:00+00:00"),
            )
            source.connection.commit()
            await ConnectionRepository(
                source,
                scope=LeaseScope(
                    tenant_id="tenant",
                    household_id="home-a",
                    deployment_id="edge",
                ),
            ).save(_profile())
            report = await migrate_sqlite_to_postgres(source, destination)

            assert report.table_counts["devices"] == 1
            assert report.payload_digests["devices"]
            assert report.table_counts["connections"] == 1
            assert report.table_counts["connection_ownership"] == 1
            assert report.payload_digests["connections"]
            assert report.payload_digests["connection_ownership"]
        finally:
            await source.close()
            await destination.close()


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_postgres_open_existing_does_not_create_application_tables() -> None:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        database = PostgresDatabase(container.get_connection_url(driver=None))
        import psycopg

        with pytest.raises(RuntimeError, match="not provisioned"):
            await database.open_existing()
        try:
            with psycopg.connect(container.get_connection_url(driver=None)) as raw:
                assert (
                    raw.execute("SELECT to_regclass('public.connection_ownership')").fetchone()[0]
                    is None
                )
        finally:
            await database.close()


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_runtime_floor_transfer_preserves_source_checkpoint_and_refuses_nonempty(
    tmp_path: Path,
) -> None:
    import json

    from testcontainers.community.postgres import PostgresContainer

    from domoai.persistence.postgres_migration import MigrationConflict
    from tests.integration.test_runtime_schema_compatibility import historical_payload

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        source = SQLiteDatabase(tmp_path / "source.sqlite")
        destination = PostgresDatabase(container.get_connection_url(driver=None))
        await source.initialize()
        await destination.initialize()
        try:
            source.connection.execute(
                "UPDATE runtime_contract_floor SET checkpoint_id='checkpoint-after-drain'"
            )
            for state in ("draft", "ready", "approved", "executing", "completed", "unknown"):
                raw = historical_payload(status=state, record_id=state)
                source.connection.execute(
                    "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
                    (state, raw, state, "2026-09-11T00:00:00Z"),
                )
            source.connection.commit()
            from tests.fixtures.runtime_schema_compatibility import seed_related_history

            related = await seed_related_history(source)
            original = source.connection.execute(
                "SELECT id,payload,status FROM plans ORDER BY id"
            ).fetchall()
            floor = source.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
            backup = tmp_path / "checkpoint.sqlite"
            assert source.backup_to(backup).integrity_check == "ok"
            report = await migrate_sqlite_to_postgres(source, destination)
            assert report.table_counts["runtime_contract_floor"] == 1
            for table, expected in related.items():
                assert (
                    destination.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    == expected
                )
                assert report.table_counts[table] == len(expected)

            assert (
                destination.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
                == floor
            )
            assert (
                destination.connection.execute(
                    "SELECT id,payload,status FROM plans ORDER BY id"
                ).fetchall()
                == original
            )
            await destination.close()
            await destination.open_existing()
            assert (
                destination.connection.execute(
                    "SELECT id,payload,status FROM plans ORDER BY id"
                ).fetchall()
                == original
            )
            with pytest.raises(MigrationConflict):
                await migrate_sqlite_to_postgres(source, destination)
            assert (
                source.connection.execute(
                    "SELECT id,payload,status FROM plans ORDER BY id"
                ).fetchall()
                == original
            )
            assert json.loads(original[-1][1])["schema_version"] == "v1"
        finally:
            await source.close()
            await destination.close()


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_future_postgres_floor_rejected_before_ddl_and_on_existing_open() -> None:
    import psycopg
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        dsn = container.get_connection_url(driver=None)
        with psycopg.connect(dsn) as raw:
            raw.execute(
                "CREATE TABLE runtime_contract_floor(singleton INTEGER, minimum_reader INTEGER, "
                "checkpoint_id TEXT, created_at TEXT)"
            )
            raw.execute(
                "INSERT INTO runtime_contract_floor VALUES(1,999,'future-checkpoint','2026-09-11')"
            )
        for method in ("initialize", "open_existing"):
            database = PostgresDatabase(dsn)
            with pytest.raises(RuntimeError, match="runtime contract floor"):
                await getattr(database, method)()
            assert database._postgres_connection is None
            with psycopg.connect(dsn) as raw:
                assert (
                    raw.execute("SELECT to_regclass('public.schema_migrations')").fetchone()[0]
                    is None
                )
                assert (
                    raw.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[0]
                    == 999
                )


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_floor_transfer_refuses_downgrade_and_rolls_back_checkpoint_on_failure(
    tmp_path,
) -> None:
    import psycopg
    from testcontainers.community.postgres import PostgresContainer

    from domoai.persistence.postgres_migration import MigrationConflict

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        source = SQLiteDatabase(tmp_path / "failure.sqlite")
        destination = PostgresDatabase(container.get_connection_url(driver=None))
        await source.initialize()
        await destination.initialize()
        try:
            original_floor = destination.connection.execute(
                "SELECT * FROM runtime_contract_floor"
            ).fetchone()
            source.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=1")
            source.connection.commit()
            with pytest.raises(MigrationConflict, match="downgraded"):
                await migrate_sqlite_to_postgres(source, destination)
            source.connection.execute(
                "UPDATE runtime_contract_floor SET minimum_reader=3,"
                "checkpoint_id='source-checkpoint'"
            )
            source.connection.execute("INSERT INTO devices VALUES('fail','{}','now')")
            source.connection.commit()
            destination.connection.execute("ALTER TABLE devices ADD CHECK(id <> 'fail')")
            destination.connection.commit()
            with pytest.raises(psycopg.errors.CheckViolation):
                await migrate_sqlite_to_postgres(source, destination)
            assert (
                destination.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
                == original_floor
            )
            assert destination.connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0
            assert source.connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 1
        finally:
            await source.close()
            await destination.close()


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_postgres_floor_elevation_is_atomic_and_serialized() -> None:
    import asyncio

    import psycopg
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        dsn = container.get_connection_url(driver=None)
        seed = PostgresDatabase(dsn)
        await seed.initialize()
        seed.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=1")
        seed.connection.commit()
        original = seed.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
        await seed.close()
        await seed.open_existing()
        assert (
            seed.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == original
        )
        await seed.close()
        with psycopg.connect(dsn) as raw:
            raw.execute(
                "CREATE FUNCTION reject_marker() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'injected marker failure'; END $$"
            )
            raw.execute(
                "CREATE TRIGGER reject_marker BEFORE INSERT ON schema_migrations "
                "FOR EACH ROW EXECUTE FUNCTION reject_marker()"
            )
        with pytest.raises(psycopg.errors.RaiseException, match="injected marker failure"):
            await seed.initialize()
        assert seed._postgres_connection is None
        with psycopg.connect(dsn) as raw:
            assert raw.execute("SELECT * FROM runtime_contract_floor").fetchone() == original
            raw.execute("DROP TRIGGER reject_marker ON schema_migrations")

        def initialize_writer():
            async def run():
                writer = PostgresDatabase(dsn)
                try:
                    await writer.initialize()
                    return writer.connection.execute(
                        "SELECT * FROM runtime_contract_floor"
                    ).fetchone()
                finally:
                    await writer.close()

            return asyncio.run(run())

        results = await asyncio.gather(*[asyncio.to_thread(initialize_writer) for _ in range(2)])
        assert results == [(1, 3, *original[2:])] * 2
