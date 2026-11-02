"""Additive preference storage survives upgrade, downgrade and reupgrade."""

import pytest

from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_upgrade_from_024_preserves_existing_tables(tmp_path):
    import shutil

    from domoai.persistence.sqlite import MIGRATIONS_DIR

    prior = tmp_path / "prior-migrations"
    prior.mkdir()
    for migration in MIGRATIONS_DIR.glob("*.sql"):
        if migration.name < "025":
            shutil.copyfile(migration, prior / migration.name)
    database = SQLiteDatabase(tmp_path / "prior.sqlite")
    await database.initialize(migrations_dir=prior)
    assert database.connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='household_preference_heads'"
    ).fetchone()[0] == 0
    await database.close()
    await database.initialize()
    try:
        assert database.connection.execute(
            "SELECT COUNT(*) FROM household_preference_heads"
        ).fetchone()[0] == 0
        assert database.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='plans'"
        ).fetchone()[0] == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_additive_preference_tables_and_reinitialization(tmp_path):
    db = SQLiteDatabase(tmp_path / "home.sqlite")
    await db.initialize()
    try:
        tables = {
            row[0]
            for row in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "household_preference_heads",
            "household_preferences",
            "household_preference_history",
            "household_preference_requests",
        } <= tables
        db.connection.execute(
            "INSERT INTO household_preference_heads "
            "(tenant_id, household_id, generation, revision) "
            "VALUES ('tenant-a', 'home-a', 'generation-a', 7)"
        )
        db.connection.commit()
    finally:
        await db.close()
    await db.initialize()
    try:
        assert db.connection.execute(
            "SELECT revision FROM household_preference_heads"
        ).fetchone() == (7,)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_real_postgres_cas_migration_and_restore_generation(tmp_path):
    """Run like test_phase3_external_postgres: Docker postgres:16-alpine, isolated DB."""
    from concurrent.futures import ThreadPoolExecutor

    import docker
    from testcontainers.community.postgres import PostgresContainer

    from domoai.domain.errors import DomainError
    from domoai.persistence.household_preferences import rotate_restored_preference_generations
    from domoai.persistence.postgres import PostgresDatabase
    from domoai.persistence.postgres_migration import migrate_sqlite_to_postgres
    from tests.integration.test_household_preference_persistence import (
        compete,
        repository,
        set_value,
    )

    try:
        docker.from_env().ping()
    except Exception:
        pytest.skip("Docker is unavailable; PostgreSQL CAS is unproved")
    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        first = PostgresDatabase(container.get_connection_url(driver=None))
        second = PostgresDatabase(container.get_connection_url(driver=None))
        source = SQLiteDatabase(tmp_path / "source.sqlite")
        await source.initialize()
        await first.initialize()
        await second.initialize()
        try:
            source_repo = await repository(source)
            initial, _ = await source_repo.snapshot()
            confirmed = await set_value(source_repo, initial)
            report = await migrate_sqlite_to_postgres(source, first)
            assert report.table_counts["household_preference_requests"] == 1
            a, b = await repository(first), await repository(second)
            assert (await a.snapshot())[0] == confirmed.version
            with ThreadPoolExecutor(2) as pool:
                outcomes = list(
                    pool.map(
                        lambda pair: compete(pair[0], confirmed.version, pair[1]),
                        [(a, "resident-a"), (b, "resident-b")],
                    )
                )
            assert sum(not isinstance(value, DomainError) for value in outcomes) == 1
            assert (await a.snapshot())[0].revision == 2
            assert len((await a.history())[1]) == 2
            before_failure, _ = await a.snapshot()
            first.connection.execute(
                "CREATE FUNCTION reject_preference_insert() RETURNS trigger LANGUAGE plpgsql "
                "AS 'BEGIN RAISE EXCEPTION ''injected preference failure''; END'"
            )
            first.connection.execute(
                "CREATE TRIGGER reject_preference_history BEFORE INSERT "
                "ON household_preference_history FOR EACH ROW "
                "EXECUTE FUNCTION reject_preference_insert()"
            )
            first.connection.commit()
            with pytest.raises(Exception, match="injected preference failure"):
                await set_value(a, before_failure, key="rollback")
            assert (await a.snapshot())[0] == before_failure
            assert len((await a.history())[1]) == 2
            first.connection.execute(
                "DROP TRIGGER reject_preference_history ON household_preference_history"
            )
            first.connection.commit()
            other = await repository(second, "home-b")
            other_initial, _ = await other.snapshot()
            other_confirmed = await set_value(other, other_initial)
            assert other_confirmed.version.revision == 1
            assert (await a.snapshot())[0] == before_failure
            first.connection.execute("BEGIN")
            rotate_restored_preference_generations(first.connection)
            first.connection.commit()
            rotated, slots = await a.snapshot()
            assert rotated.generation != confirmed.version.generation and rotated.revision == 2
            with pytest.raises(DomainError) as fenced:
                await set_value(a, initial)
            assert fenced.value.code == "preference_conflict"
            assert len(slots) == 1
        finally:
            await source.close()
            await first.close()
            await second.close()
