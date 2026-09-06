from __future__ import annotations

from pathlib import Path

import pytest

from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.postgres_migration import migrate_sqlite_to_postgres
from domoai.persistence.sqlite import SQLiteDatabase


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
            report = await migrate_sqlite_to_postgres(source, destination)

            assert report.table_counts["devices"] == 1
            assert report.payload_digests["devices"]
        finally:
            await source.close()
            await destination.close()
