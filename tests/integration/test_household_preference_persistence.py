"""Real database transactions, restart, CAS and privacy receipts."""

import asyncio
import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest

from domoai.domain.errors import DomainError
from domoai.domain.household_preferences import (
    PreferenceMutation,
    PreferenceRestore,
    PreferenceSlotMutation,
)
from domoai.persistence.sqlite import SQLiteDatabase
from tests.fixtures.household_preferences import NOW, mutation


async def repository(db, household="home-a"):
    module = importlib.import_module("domoai.persistence.household_preferences")
    repo = module.HouseholdPreferenceRepository(db, tenant_id="tenant-a", household_id=household)
    await repo.bootstrap()
    return repo


async def set_value(repo, version, principal="resident-1", key="request-1"):
    request = PreferenceMutation.model_validate(
        mutation(
            version.model_dump(mode="json"),
            idempotency_key=key,
        )
    )
    return await repo.mutate(operation="set", request=request, principal_id=principal, now=NOW)


def compete(repo, version, principal):
    try:
        return asyncio.run(set_value(repo, version, principal))
    except DomainError as error:
        return error


@pytest.mark.asyncio
async def test_cas_two_connections_and_idempotent_lost_response(tmp_path):
    first = SQLiteDatabase(tmp_path / "home.sqlite")
    second = SQLiteDatabase(tmp_path / "home.sqlite")
    await first.initialize()
    await second.initialize()
    try:
        a, b = await repository(first), await repository(second)
        version, slots = await a.snapshot()
        assert version.revision == 0 and slots == []
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(
                pool.map(
                    lambda pair: compete(pair[0], version, pair[1]),
                    [(a, "resident-1"), (b, "resident-2")],
                )
            )
        assert sum(not isinstance(value, DomainError) for value in outcomes) == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_retry_and_failure_rollback(tmp_path):
    db = SQLiteDatabase(tmp_path / "home.sqlite")
    await db.initialize()
    try:
        repo = await repository(db)
        version, _ = await repo.snapshot()
        result = await set_value(repo, version)
        assert await set_value(repo, version) == result
        assert (await repo.snapshot())[0].revision == 1
        db.connection.execute(
            "CREATE TRIGGER reject_preference_history BEFORE INSERT ON "
            "household_preference_history BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
        db.connection.commit()
        with pytest.raises(Exception, match="injected"):
            await set_value(repo, result.version, key="failed")
        assert (await repo.snapshot())[0].revision == 1
        assert (
            db.connection.execute("SELECT COUNT(*) FROM household_preference_requests").fetchone()[
                0
            ]
            == 1
        )
        assert (
            db.connection.execute("SELECT COUNT(*) FROM household_preference_history").fetchone()[0]
            == 1
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_restore_and_erase_redact_all_retained_payloads(tmp_path):
    db = SQLiteDatabase(tmp_path / "home.sqlite")
    await db.initialize()
    try:
        repo = await repository(db)
        version, _ = await repo.snapshot()
        original = await set_value(repo, version)
        slot_id = original.preference.preference_id
        delete_request = PreferenceSlotMutation(
            expected_version=original.version, preference_id=slot_id, idempotency_key="delete"
        )
        deleted = await repo.mutate(
            operation="delete",
            principal_id="resident-1",
            now=NOW,
            request=delete_request,
        )
        assert deleted.preference.deleted is True
        before_restore = (await repo.history())[1]
        assert [entry.operation for entry in before_restore] == ["set", "delete"]
        assert before_restore[0].snapshot == original.preference
        assert before_restore[1].snapshot == deleted.preference
        restore_request = PreferenceRestore(
            expected_version=deleted.version,
            preference_id=slot_id,
            idempotency_key="restore",
            source_revision=1,
            valid_from="2026-09-12T06:00:00+00:00",
            valid_until="2026-09-12T08:00:00+00:00",
            timezone="UTC",
            provenance={"source_kind": "manual", "source_ref": "new-validity"},
        )
        with pytest.raises(DomainError) as conflict:
            await repo.mutate(
                operation="restore",
                principal_id="resident-2",
                now=NOW,
                request=restore_request.model_copy(update={"expected_version": original.version}),
            )
        assert conflict.value.code == "preference_conflict"
        assert (await repo.snapshot())[0] == deleted.version
        assert (await repo.history())[1] == before_restore
        restored = await repo.mutate(
            operation="restore",
            principal_id="resident-2",
            now=NOW,
            request=restore_request,
        )
        assert restored.version.revision == 3
        assert restored.version.generation == original.version.generation
        assert restored.preference.preference.value is False
        assert restored.preference.preference.valid_from == datetime(2026, 9, 12, 6, tzinfo=UTC)
        assert restored.preference.preference.valid_until == datetime(2026, 9, 12, 8, tzinfo=UTC)
        assert restored.preference.preference.timezone == "UTC"
        assert restored.preference.provenance.source_ref == "new-validity"
        restored_history = (await repo.history())[1]
        assert restored_history[:2] == before_restore
        assert restored_history[2].source_revision == 1
        assert restored_history[2].snapshot == restored.preference
        erased = await repo.mutate(
            operation="erase",
            principal_id="resident-1",
            now=NOW,
            request=PreferenceSlotMutation(
                expected_version=restored.version, preference_id=slot_id, idempotency_key="erase"
            ),
        )
        assert erased.version.revision == 4 and erased.backups_may_retain_data
        retry = await set_value(repo, version)
        assert retry.version == original.version and retry.preference is None
        for operation, request, principal, expected in (
            ("delete", delete_request, "resident-1", deleted.version),
            ("restore", restore_request, "resident-2", restored.version),
        ):
            retry = await repo.mutate(
                operation=operation, request=request, principal_id=principal, now=NOW
            )
            assert retry.version == expected and retry.preference is None
        receipts = db.connection.execute(
            "SELECT response FROM household_preference_requests ORDER BY idempotency_key"
        ).fetchall()
        assert len(receipts) == 4
        assert all(json.loads(row[0])["preference"] is None for row in receipts)
        entries = await repo.history()
        assert len(entries[1]) == 4
        assert all(entry.snapshot is None for entry in entries[1])
        slot = (await repo.snapshot())[1][0]
        assert slot.preference is None and slot.provenance is None
        with pytest.raises(DomainError) as missing:
            await repo.mutate(
                operation="restore",
                principal_id="resident-1",
                now=NOW,
                request=PreferenceRestore(
                    expected_version=erased.version,
                    preference_id=slot_id,
                    idempotency_key="after-erase",
                    source_revision=1,
                    valid_from=NOW,
                    valid_until=None,
                    timezone="UTC",
                    provenance={"source_kind": "manual"},
                ),
            )
        assert missing.value.code == "history_unavailable"
        assert (await repo.snapshot())[0] == erased.version
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["retained_receipt", "erase_receipt"])
async def test_erase_failure_rolls_back_head_slot_history_and_all_receipts(
    tmp_path, failure_boundary
):
    db = SQLiteDatabase(tmp_path / "erase-rollback.sqlite")
    await db.initialize()
    try:
        repo = await repository(db)
        initial, _ = await repo.snapshot()
        first = await set_value(repo, initial)
        second = await set_value(repo, first.version, principal="resident-2", key="second")
        request = PreferenceSlotMutation(
            expected_version=second.version,
            preference_id=first.preference.preference_id,
            idempotency_key="erase",
        )
        tables = (
            "household_preference_heads",
            "household_preferences",
            "household_preference_history",
            "household_preference_requests",
        )
        before = {
            table: [tuple(row) for row in db.connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        # Real DB failure during receipt redaction or after all redactions and
        # the new slot/history have been written, before commit can succeed.
        event = "UPDATE OF response" if failure_boundary == "retained_receipt" else "INSERT"
        db.connection.execute(
            f"CREATE TRIGGER reject_erase_receipt BEFORE {event} "
            "ON household_preference_requests "
            "BEGIN SELECT RAISE(ABORT, 'erase-injected'); END"
        )
        db.connection.commit()
        with pytest.raises(sqlite3.IntegrityError, match="erase-injected"):
            await repo.mutate(
                operation="erase", request=request, principal_id="resident-1", now=NOW
            )
        after = {
            table: [tuple(row) for row in db.connection.execute(f"SELECT * FROM {table}")]
            for table in tables
        }
        assert after == before
        assert (await repo.snapshot())[0] == second.version
        assert await set_value(repo, initial) == first
        assert await set_value(repo, first.version, principal="resident-2", key="second") == second

        db.connection.execute("DROP TRIGGER reject_erase_receipt")
        db.connection.commit()
        erased = await repo.mutate(
            operation="erase", request=request, principal_id="resident-1", now=NOW
        )
        assert erased.version.revision == 3 and erased.preference is None
        slot = (await repo.snapshot())[1][0]
        assert slot.erased and slot.preference is None and slot.provenance is None
        history = (await repo.history())[1]
        assert len(history) == 3 and all(entry.snapshot is None for entry in history)
        receipts = db.connection.execute("SELECT response FROM household_preference_requests")
        payloads = [json.loads(row[0]) for row in receipts]
        assert len(payloads) == 3 and all(payload["preference"] is None for payload in payloads)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_read_watermark_restart_and_distinct_household_are_independent(tmp_path):
    db = SQLiteDatabase(tmp_path / "home.sqlite")
    await db.initialize()
    repo = await repository(db)
    other = await repository(db, "home-b")
    initial, _ = await repo.snapshot()
    other_initial, _ = await other.snapshot()
    confirmed = await set_value(repo, initial)
    assert confirmed.task_reevaluation == "consumer_unavailable"
    before = db.connection.total_changes
    for _ in range(3):
        version, slots = await repo.snapshot()
        assert version == confirmed.version and slots[0].version == version
        assert (await other.snapshot())[0] == other_initial
    assert db.connection.total_changes == before
    await db.close()
    await db.initialize()
    try:
        restarted = await repository(db)
        assert (await restarted.snapshot())[0] == confirmed.version
        assert await set_value(restarted, initial) == confirmed
        another = await set_value(await repository(db, "home-b"), other_initial)
        assert another.version.revision == 1
        assert (await restarted.snapshot())[0] == confirmed.version
    finally:
        await db.close()
