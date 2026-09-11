from __future__ import annotations

import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from domoai.domain.connections import ConnectionProfile
from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import MIGRATIONS_DIR, SQLiteDatabase


def _profile() -> ConnectionProfile:
    timestamp = datetime(2026, 1, 1, 12, tzinfo=UTC)
    return ConnectionProfile(
        id=UUID("0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1001"),
        connector_id="home_assistant",
        unique_id="ha-main",
        endpoint={"url": "http://homeassistant.local:8123", "tls": False},
        options={"verify_tls": False},
        credential_ref="env:DOMOAI_HA_TOKEN",
        mapping_ref=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


@pytest.mark.asyncio
async def test_fresh_schema_registers_connection_table_columns_and_indexes(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "fresh.sqlite3")
    await database.initialize()
    try:
        migrations = {
            row[0]
            for row in database.connection.execute(
                "SELECT filename FROM schema_migrations"
            ).fetchall()
        }
        assert "018_connection_manager.sql" in migrations

        columns = [row[1] for row in database.connection.execute("PRAGMA table_info(connections)")]
        assert columns == [
            "id",
            "connector_id",
            "unique_id",
            "endpoint_json",
            "options_json",
            "credential_ref",
            "mapping_ref",
            "enabled",
            "schema_version",
            "created_at",
            "updated_at",
            "credential_transfer_id",
        ]
        indexes = {
            row[1]
            for row in database.connection.execute("PRAGMA index_list(connections)").fetchall()
        }
        assert {"idx_connections_connector_id", "idx_connections_enabled"}.issubset(indexes)
        create_sql = database.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'connections'"
        ).fetchone()[0]
        assert "UNIQUE(connector_id,unique_id)" in create_sql.replace(" ", "")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_additive_migration_preserves_existing_rows_and_is_restart_idempotent(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade.sqlite3"
    older_migrations = tmp_path / "older-migrations"
    older_migrations.mkdir()
    shutil.copyfile(MIGRATIONS_DIR / "001_initial.sql", older_migrations / "001_initial.sql")

    old_database = SQLiteDatabase(database_path)
    await old_database.initialize(migrations_dir=older_migrations)
    old_database.connection.execute(
        "INSERT INTO devices (id, payload, updated_at) VALUES (?, ?, ?)",
        ("legacy-device", "{}", "2026-01-01T12:00:00+00:00"),
    )
    old_database.connection.commit()
    await old_database.close()

    upgraded = SQLiteDatabase(database_path)
    await upgraded.initialize()
    try:
        assert upgraded.connection.execute(
            "SELECT payload FROM devices WHERE id = ?", ("legacy-device",)
        ).fetchone() == ("{}",)
        assert upgraded.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename = ?",
            ("018_connection_manager.sql",),
        ).fetchone() == (1,)
        count_before_restart = upgraded.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone()[0]
    finally:
        await upgraded.close()

    restarted = SQLiteDatabase(database_path)
    await restarted.initialize()
    try:
        assert restarted.connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone() == (count_before_restart,)
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_connection_profile_survives_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.sqlite3"
    first_database = SQLiteDatabase(database_path)
    await first_database.initialize()
    profile = _profile()
    await ConnectionRepository(
        first_database,
        scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
    ).save(profile)
    await first_database.close()

    second_database = SQLiteDatabase(database_path)
    await second_database.initialize()
    try:
        assert (
            await ConnectionRepository(
                second_database,
                scope=LeaseScope(
                    tenant_id="default", household_id="default", deployment_id="default"
                ),
            ).get(profile.id)
            == profile
        )
    finally:
        await second_database.close()


@pytest.mark.asyncio
async def test_online_backup_contains_connection_rows_and_migration_ledger(tmp_path: Path) -> None:
    source = SQLiteDatabase(tmp_path / "source.sqlite3")
    await source.initialize()
    try:
        profile = _profile()
        await ConnectionRepository(
            source,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        ).save(profile)
        backup_path = tmp_path / "backup.sqlite3"

        result = source.backup_to(backup_path)

        assert "018_connection_manager.sql" in result.schema_migrations
        restored = sqlite3.connect(backup_path)
        try:
            assert restored.execute(
                "SELECT COUNT(*) FROM connections WHERE id = ?", (str(profile.id),)
            ).fetchone() == (1,)
            assert restored.execute(
                "SELECT filename FROM schema_migrations WHERE filename = ?",
                ("018_connection_manager.sql",),
            ).fetchone() == ("018_connection_manager.sql",)
            assert restored.execute(
                "SELECT tenant_id, household_id, deployment_id FROM connection_ownership "
                "WHERE connection_id = ?",
                (str(profile.id),),
            ).fetchone() == ("default", "default", "default")
            assert restored.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        finally:
            restored.close()
    finally:
        await source.close()
