import json
from datetime import UTC, datetime

import pytest

from domoai.application.privacy import PrivacyService
from domoai.domain.models import AuthorityContext, SourceRef, StateSnapshot, StateStatus
from domoai.domain.privacy import HouseholdDataPolicy, PrivacyCategory
from domoai.persistence.privacy import SQLitePrivacyStore
from domoai.persistence.repositories import StateHistoryRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock


def _authority(household: str) -> AuthorityContext:
    return AuthorityContext(
        tenant_id="tenant-a",
        household_id=household,
        household_ids=[household],
        principal_id=f"owner-{household}",
        roles=["owner"],
    )


@pytest.mark.asyncio
async def test_sqlite_privacy_store_exports_and_deletes_only_one_household(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "privacy.sqlite3")
    await database.initialize()
    try:
        for plan_id, household_id in (("plan-a", "home-a"), ("plan-b", "home-b")):
            payload = {
                "authority": _authority(household_id).model_dump(mode="json"),
                "id": plan_id,
            }
            database.connection.execute(
                "INSERT INTO plans (id, payload, updated_at) VALUES (?, ?, ?)",
                (plan_id, json.dumps(payload), "2026-09-04T00:00:00+00:00"),
            )
        database.connection.commit()

        audit_events: list[dict] = []
        service = PrivacyService(
            SQLitePrivacyStore(database),
            audit=lambda **event: audit_events.append(event),
        )
        policy = HouseholdDataPolicy(
            authority=_authority("home-a"),
            exportable_categories=[PrivacyCategory.PLANS],
            deletable_categories=[PrivacyCategory.PLANS],
            immutable_categories=[PrivacyCategory.AUDIT],
            retention_days=90,
        )

        exported = await service.export(
            policy, _authority("home-a"), categories=[PrivacyCategory.PLANS]
        )
        deleted = await service.delete(
            policy,
            _authority("home-a"),
            categories=[PrivacyCategory.PLANS],
            request_id="privacy-request-1",
        )

        assert exported.records == [
            {"authority": _authority("home-a").model_dump(mode="json"), "id": "plan-a"}
        ]
        assert deleted.deleted_counts == {"plans": 1}
        remaining = await SQLitePrivacyStore(database).export_category(
            PrivacyCategory.PLANS, "home-b"
        )
        assert [record["id"] for record in remaining] == ["plan-b"]
        assert audit_events[0]["payload"] == {
            "household_id": "home-a",
            "categories": ["plans"],
            "deleted_counts": {"plans": 1},
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sqlite_privacy_store_scopes_recurring_schedules_by_authority_column(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "recurring-privacy.sqlite3")
    await database.initialize()
    try:
        for schedule_id, household_id in (("schedule-a", "home-a"), ("schedule-b", "home-b")):
            database.connection.execute(
                """INSERT INTO recurring_schedules
                   (schedule_id, template_payload, recurrence_payload, next_execute_at,
                    status, updated_at, authority_payload)
                   VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                (
                    schedule_id,
                    json.dumps([{"command": "turn_on"}]),
                    json.dumps({"time": "07:00", "timezone": "Europe/Madrid"}),
                    "2026-09-05T07:00:00+00:00",
                    "2026-09-04T00:00:00+00:00",
                    json.dumps(_authority(household_id).model_dump(mode="json")),
                ),
            )
        database.connection.commit()

        service = PrivacyService(SQLitePrivacyStore(database))
        policy = HouseholdDataPolicy(
            authority=_authority("home-a"),
            exportable_categories=[PrivacyCategory.SCHEDULES],
            deletable_categories=[PrivacyCategory.SCHEDULES],
            retention_days=90,
        )

        exported = await service.export(
            policy, _authority("home-a"), categories=[PrivacyCategory.SCHEDULES]
        )
        deleted = await service.delete(
            policy,
            _authority("home-a"),
            categories=[PrivacyCategory.SCHEDULES],
            request_id="schedule-delete-1",
        )

        assert [record["schedule_id"] for record in exported.records] == ["schedule-a"]
        assert deleted.deleted_counts == {"schedules": 1}
        remaining = await SQLitePrivacyStore(database).export_category(
            PrivacyCategory.SCHEDULES, "home-b"
        )
        assert [record["schedule_id"] for record in remaining] == ["schedule-b"]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_sqlite_privacy_store_keeps_legacy_rows_in_default_household_scope(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy-privacy.sqlite3")
    await database.initialize()
    try:
        database.connection.execute(
            "INSERT INTO plans (id, payload, updated_at) VALUES (?, ?, ?)",
            ("legacy-plan", json.dumps({"id": "legacy-plan"}), "2026-09-04T00:00:00+00:00"),
        )
        database.connection.commit()
        service = PrivacyService(SQLitePrivacyStore(database))
        policy = HouseholdDataPolicy(
            authority=_authority("default"),
            exportable_categories=[PrivacyCategory.PLANS],
            deletable_categories=[PrivacyCategory.PLANS],
            retention_days=90,
        )

        exported = await service.export(
            policy, _authority("default"), categories=[PrivacyCategory.PLANS]
        )

        assert exported.records == [{"id": "legacy-plan"}]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_state_privacy_export_and_delete_include_history_without_crossing_households(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "history-privacy.sqlite3")
    await database.initialize()
    try:
        now = datetime(2026, 9, 5, 12, tzinfo=UTC)
        home_a = StateHistoryRepository(database, household_id="home-a", clock=FixedClock(now))
        home_b = StateHistoryRepository(database, household_id="home-b", clock=FixedClock(now))
        snapshot = StateSnapshot(
            device_id="light.kitchen",
            capability="brightness",
            value=42,
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="light.kitchen"),
        )
        await home_a.append([snapshot])
        await home_b.append([snapshot.model_copy(update={"value": 7})])
        service = PrivacyService(SQLitePrivacyStore(database))
        policy = HouseholdDataPolicy(
            authority=_authority("home-a"),
            exportable_categories=[PrivacyCategory.STATE],
            deletable_categories=[PrivacyCategory.STATE],
            retention_days=90,
        )

        exported = await service.export(
            policy, _authority("home-a"), categories=[PrivacyCategory.STATE]
        )
        deleted = await service.delete(
            policy,
            _authority("home-a"),
            categories=[PrivacyCategory.STATE],
            request_id="state-delete-1",
        )

        assert exported.record_count == 1
        assert exported.records[0]["snapshot"]["value"] == 42
        assert deleted.deleted_counts == {"state": 1}
        assert await home_a.list_history() == []
        assert [item.snapshot.value for item in await home_b.list_history()] == [7]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_privacy_export_paginates_large_household_data_with_bound_and_cursor(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "large-privacy.sqlite3")
    await database.initialize()
    try:
        rows = [
            (
                f"large-plan-{index}",
                json.dumps(
                    {
                        "authority": _authority("home-a").model_dump(mode="json"),
                        "id": f"large-plan-{index}",
                    }
                ),
                "2026-09-04T00:00:00+00:00",
            )
            for index in range(5000)
        ]
        database.connection.executemany(
            "INSERT INTO plans (id, payload, updated_at) VALUES (?, ?, ?)", rows
        )
        database.connection.commit()
        service = PrivacyService(SQLitePrivacyStore(database), page_limit=257)
        policy = HouseholdDataPolicy(
            authority=_authority("home-a"),
            exportable_categories=[PrivacyCategory.PLANS],
            retention_days=90,
        )

        pages = []
        cursor = None
        while True:
            page = await service.export(
                policy,
                _authority("home-a"),
                categories=[PrivacyCategory.PLANS],
                cursor=cursor,
            )
            assert len(page.records) <= 257
            pages.extend(page.records)
            cursor = page.next_cursor
            if cursor is None:
                break

        assert len(pages) == 5000
        assert len({record["id"] for record in pages}) == 5000
        assert page.total_record_count == 5000
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_privacy_export_rejects_tampered_or_cross_household_cursor(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "cursor-privacy.sqlite3")
    await database.initialize()
    try:
        database.connection.execute(
            "INSERT INTO plans (id, payload, updated_at) VALUES (?, ?, ?)",
            (
                "cursor-plan",
                json.dumps({"authority": _authority("home-a").model_dump(mode="json")}),
                "2026-09-04T00:00:00+00:00",
            ),
        )
        database.connection.execute(
            "INSERT INTO plans (id, payload, updated_at) VALUES (?, ?, ?)",
            (
                "cursor-plan-2",
                json.dumps({"authority": _authority("home-a").model_dump(mode="json")}),
                "2026-09-04T00:00:01+00:00",
            ),
        )
        database.connection.commit()
        service = PrivacyService(SQLitePrivacyStore(database), page_limit=1)
        policy_a = HouseholdDataPolicy(
            authority=_authority("home-a"),
            exportable_categories=[PrivacyCategory.PLANS],
            retention_days=90,
        )
        first = await service.export(
            policy_a, _authority("home-a"), categories=[PrivacyCategory.PLANS]
        )
        assert first.next_cursor is not None
        with pytest.raises(ValueError, match="invalid privacy export cursor"):
            await service.export(
                policy_a,
                _authority("home-a"),
                categories=[PrivacyCategory.PLANS],
                cursor="tampered.cursor",
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_state_history_retention_purges_idle_households(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "idle-retention.sqlite3")
    await database.initialize()
    try:
        clock = FixedClock(datetime(2026, 9, 1, 12, tzinfo=UTC))
        repository = StateHistoryRepository(
            database,
            household_id="home-a",
            retention_days=1,
            clock=clock,
        )
        snapshot = StateSnapshot(
            device_id="sensor.temperature",
            capability="temperature",
            value=20,
            observed_at=clock.now(),
            received_at=clock.now(),
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="sensor.temperature"),
        )
        await repository.append([snapshot])
        clock.set(datetime(2026, 9, 5, 12, tzinfo=UTC))

        assert await repository.purge_expired() == 1
        assert await repository.list_history() == []
    finally:
        await database.close()
