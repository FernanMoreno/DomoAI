from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from domoai.domain.connections import ConnectionProfile
from domoai.domain.coordination import LeaseScope
from domoai.persistence.connection_repository import ConnectionRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock

BASE_TIME = datetime(2026, 1, 1, 12, tzinfo=UTC)


async def _database(tmp_path: Path) -> SQLiteDatabase:
    database = SQLiteDatabase(tmp_path / "connections.sqlite3")
    await database.initialize()
    return database


def _profile(
    identifier: str = "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1001",
    *,
    connector_id: str = "mqtt",
    unique_id: str = "broker-main",
    created_at: datetime = BASE_TIME,
    updated_at: datetime = BASE_TIME,
) -> ConnectionProfile:
    return ConnectionProfile(
        id=UUID(identifier),
        connector_id=connector_id,
        unique_id=unique_id,
        endpoint={"host": "broker.local", "port": 1883, "tls": False},
        options={"keepalive": 60, "client_id": "domoai"},
        credential_ref="secret:0123456789abcdef",
        mapping_ref="mapping:mqtt:v1",
        created_at=created_at,
        updated_at=updated_at,
    )


@pytest.mark.asyncio
async def test_save_and_get_round_trip_the_complete_profile(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        profile = _profile()
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )

        await repository.save(profile)

        assert await repository.get(profile.id) == profile
        row = database.connection.execute(
            "SELECT endpoint_json, options_json, credential_ref FROM connections WHERE id = ?",
            (str(profile.id),),
        ).fetchone()
        assert row == (
            '{"host": "broker.local", "port": 1883, "tls": false}',
            '{"client_id": "domoai", "keepalive": 60}',
            "secret:0123456789abcdef",
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_missing_profile_lookups_return_none(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )

        assert await repository.get(UUID("0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1001")) is None
        assert await repository.get_by_unique_id("mqtt", "missing") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_get_by_unique_id_is_scoped_by_connector(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        first = _profile()
        other_connector = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1002",
            connector_id="knx",
            unique_id="broker-main",
        )
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(first)
        await repository.save(other_connector)

        assert await repository.get_by_unique_id("mqtt", "broker-main") == first
        assert await repository.get_by_unique_id("knx", "broker-main") == other_connector
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_list_all_has_deterministic_created_at_then_id_order(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        later = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1002",
            unique_id="later",
            created_at=BASE_TIME + timedelta(minutes=1),
            updated_at=BASE_TIME + timedelta(minutes=1),
        )
        earlier = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1003",
            unique_id="earlier",
            created_at=BASE_TIME,
            updated_at=BASE_TIME,
        )
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(later)
        await repository.save(earlier)

        assert await repository.list_all() == [earlier, later]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_set_enabled_updates_only_state_and_updated_at(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        clock = FixedClock(BASE_TIME)
        profile = _profile(updated_at=BASE_TIME)
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
            clock=clock,
        )
        await repository.save(profile)
        changed_at = BASE_TIME + timedelta(minutes=5)
        clock.set(changed_at)

        assert await repository.set_enabled(profile.id, False) is True
        updated = await repository.get(profile.id)
        assert updated is not None
        assert updated.model_copy(update={"enabled": False, "updated_at": changed_at}) == updated
        assert updated.endpoint == profile.endpoint
        assert updated.options == profile.options
        missing_id = UUID("0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1099")
        assert await repository.set_enabled(missing_id, False) is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_reports_affected_rows(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        profile = _profile()
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(profile)

        assert await repository.delete(profile.id) is True
        assert await repository.get(profile.id) is None
        assert await repository.delete(profile.id) is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_duplicate_profile_id_is_rejected_without_overwriting(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        original = _profile()
        duplicate_id = _profile(connector_id="knx", unique_id="other")
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(original)

        with pytest.raises(sqlite3.IntegrityError):
            await repository.save(duplicate_id)

        assert await repository.get(original.id) == original
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_duplicate_connector_unique_id_is_rejected_and_other_connector_is_allowed(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    try:
        original = _profile()
        duplicate_identity = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1002",
            unique_id=original.unique_id,
        )
        other_connector = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1003",
            connector_id="knx",
            unique_id=original.unique_id,
        )
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(original)

        with pytest.raises(sqlite3.IntegrityError):
            await repository.save(duplicate_identity)
        await repository.save(other_connector)

        assert await repository.list_all() == [original, other_connector]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_failed_duplicate_write_rolls_back_before_next_mutation(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        original = _profile()
        duplicate_identity = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1002",
            unique_id=original.unique_id,
        )
        next_profile = _profile(
            "0193b7d6-7b2b-7e8a-9f3b-2d4f2f6a1003",
            unique_id="after-rollback",
        )
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(original)

        with pytest.raises(sqlite3.IntegrityError):
            await repository.save(duplicate_identity)
        await repository.save(next_profile)

        assert await repository.get(original.id) == original
        assert await repository.get(next_profile.id) == next_profile
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_forced_sqlite_failure_rolls_back_enabled_state(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        profile = _profile()
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(profile)
        database.connection.execute(
            """CREATE TRIGGER force_connection_update_failure
               AFTER UPDATE OF enabled ON connections
               BEGIN
                   SELECT RAISE(ABORT, 'forced connection update failure');
               END"""
        )
        database.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="forced connection update failure"):
            await repository.set_enabled(profile.id, False)

        assert await repository.get(profile.id) == profile
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reads_revalidate_persisted_json(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    try:
        profile = _profile()
        repository = ConnectionRepository(
            database,
            scope=LeaseScope(tenant_id="default", household_id="default", deployment_id="default"),
        )
        await repository.save(profile)
        database.connection.execute(
            "UPDATE connections SET endpoint_json = ? WHERE id = ?",
            ('{"password": "must-not-be-accepted"}', str(profile.id)),
        )
        database.connection.commit()

        with pytest.raises(ValidationError):
            await repository.get(profile.id)
    finally:
        await database.close()


def test_repository_has_no_unscoped_constructor(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "unused.db")
    with pytest.raises(TypeError):
        ConnectionRepository(database)


@pytest.mark.asyncio
async def test_two_households_cannot_read_or_mutate_each_others_profiles(tmp_path: Path) -> None:
    from domoai.domain.coordination import LeaseScope

    database = await _database(tmp_path)
    scope = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
    owner = ConnectionRepository(database, scope=scope)
    other = ConnectionRepository(
        database, scope=scope.model_copy(update={"household_id": "home-b"})
    )
    profile = _profile()
    try:
        await owner.save(profile)
        assert await other.list_all() == []
        assert await other.get(profile.id) is None
        assert await other.get_by_unique_id(profile.connector_id, profile.unique_id) is None
        assert not await other.set_enabled(profile.id, True)
        assert not await other.set_credential_ref(profile.id, "secret:foreign")
        assert not await other.update(profile.model_copy(update={"unique_id": "stolen"}))
        assert not await other.delete(profile.id)
        assert await owner.get(profile.id) == profile
    finally:
        await database.close()
