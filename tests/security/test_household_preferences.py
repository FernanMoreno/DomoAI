"""Authority is verified independently of caller preference content."""

import asyncio
import logging
from datetime import UTC, datetime

import pytest
from tests.fixtures.household_preferences import authority, clock, devices, mutation, preference

from domoai.application.authority import AuthorityPolicy
from domoai.application.household_preferences import HouseholdPreferenceService
from domoai.domain.errors import DomainError
from domoai.domain.household_preferences import (
    PreferenceHistoryRead,
    PreferenceMutation,
    PreferenceRead,
    PreferenceRestore,
    PreferenceSlotMutation,
)
from domoai.domain.models import PrincipalRole
from domoai.persistence.household_preferences import HouseholdPreferenceRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.registry import DeviceRegistry

OPERATIONS = {
    "get_household_preferences": {"viewer", "planner", "operator", "owner", "service"},
    "set_household_preference": {"operator", "owner", "service"},
    "delete_household_preference": {"operator", "owner", "service"},
    "get_household_preference_history": {"owner", "service"},
    "restore_household_preference": {"owner", "service"},
    "erase_household_preference": {"owner", "service"},
}


@pytest.mark.parametrize("operation,roles", OPERATIONS.items())
@pytest.mark.parametrize("role", list(PrincipalRole))
def test_explicit_role_matrix(operation, roles, role):
    policy = AuthorityPolicy(tenant_id="tenant-a", household_id="home-a")
    if role.value in roles:
        policy.authorize(authority(role=role), operation=operation)
    else:
        with pytest.raises(DomainError) as denied:
            policy.authorize(authority(role=role), operation=operation)
        assert denied.value.code == "insufficient_scope"


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "scope",
    [
        {"area_ids": ["kitchen"]},
        {"device_ids": ["water-heater"]},
        {"capabilities": ["switch.on"]},
        {"operations": ["inspect"]},
    ],
)
def test_partial_household_and_operation_scope_denied(operation, scope):
    with pytest.raises(DomainError) as denied:
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-a").authorize(
            authority(**scope),
            operation=operation,
        )
    assert denied.value.code == "insufficient_scope"


@pytest.mark.parametrize("operation", OPERATIONS)
def test_other_household_never_learns_profile_existence(operation):
    with pytest.raises(DomainError) as denied:
        AuthorityPolicy(tenant_id="tenant-a", household_id="home-b").authorize(
            authority(),
            operation=operation,
        )
    assert denied.value.code == "insufficient_scope"


@pytest.mark.asyncio
async def test_erase_keeps_sensitive_provenance_out_of_logs_audit_and_later_responses(
    tmp_path, caplog, capsys
):
    # A payload logged at any level, retained in audit, or replayed after erase
    # must fail this test. The sentinel is user content, never an actor or key.
    sensitive = "private-night-routine-7f423b02-69c1-482e-a99b"
    caplog.set_level(logging.DEBUG)
    database = SQLiteDatabase(tmp_path / "privacy.sqlite")
    await database.initialize()
    try:
        repository = HouseholdPreferenceRepository(
            database, tenant_id="tenant-a", household_id="home-a"
        )
        await repository.bootstrap()
        registry = DeviceRegistry()
        registry.load_persisted(devices())
        service = HouseholdPreferenceService(
            repository,
            policy=AuthorityPolicy(tenant_id="tenant-a", household_id="home-a"),
            registry=registry,
            clock=clock(),
            inventory_lock=asyncio.Lock(),
        )
        initial = await service.read(PreferenceRead(), authority())
        request = PreferenceMutation.model_validate(
            mutation(
                initial.version.model_dump(mode="json"),
                preference=preference(
                    provenance={"source_kind": "user_request", "source_ref": sensitive}
                ),
            )
        )
        saved = await service.set(request, authority())
        assert sensitive in saved.model_dump_json()
        delete_request = PreferenceSlotMutation(
            expected_version=saved.version,
            preference_id=saved.preference.preference_id,
            idempotency_key="delete-private",
        )
        deleted = await service.delete(delete_request, authority())
        restore_request = PreferenceRestore(
            expected_version=deleted.version,
            preference_id=saved.preference.preference_id,
            idempotency_key="restore-private",
            source_revision=1,
            valid_from="2026-09-12T06:00:00+00:00",
            valid_until="2026-09-12T08:00:00+00:00",
            timezone="UTC",
            provenance={"source_kind": "manual", "source_ref": sensitive},
        )
        restored = await service.restore(restore_request, authority())
        assert restored.operation == "restore" and restored.version.revision == 3
        assert restored.preference.deleted is False
        assert restored.preference.preference.valid_from == datetime(2026, 9, 12, 6, tzinfo=UTC)
        assert restored.preference.preference.valid_until == datetime(2026, 9, 12, 8, tzinfo=UTC)
        assert restored.preference.provenance.source_ref == sensitive
        first_page = await service.history(PreferenceHistoryRead(limit=1), authority())
        assert len(first_page.entries) == 1 and first_page.next_cursor is not None
        assert sensitive in first_page.entries[0].model_dump_json()
        erase_request = PreferenceSlotMutation(
            expected_version=restored.version,
            preference_id=saved.preference.preference_id,
            idempotency_key="erase-private",
        )
        erased = await service.erase(erase_request, authority())
        responses = [
            erased,
            await service.erase(erase_request, authority()),
            await service.set(request, authority()),
            await service.delete(delete_request, authority()),
            await service.read(PreferenceRead(effective_only=False), authority()),
        ]
        assert responses[2].preference is None
        assert responses[3].preference is None
        restore_replay = await service.restore(restore_request, authority())
        responses.append(restore_replay)
        assert restore_replay.operation == "restore"
        assert restore_replay.version == restored.version and restore_replay.version.revision == 3
        assert restore_replay.preference is None
        with pytest.raises(DomainError) as missing:
            await service.restore(
                restore_request.model_copy(
                    update={
                        "expected_version": erased.version,
                        "idempotency_key": "fresh-restore-after-erase",
                    }
                ),
                authority(),
            )
        assert missing.value.code == "history_unavailable"
        responses.append(missing.value.as_detail())
        with pytest.raises(DomainError) as stale:
            await service.history(
                PreferenceHistoryRead(limit=1, cursor=first_page.next_cursor), authority()
            )
        assert stale.value.code == "stale_cursor"
        responses.append(stale.value.as_detail())
        for error in (missing.value, stale.value):
            assert sensitive not in str(error)
            assert sensitive not in repr(error.details)

        # Restart pagination against the new version; every bounded page is safe.
        cursor = None
        revisions = []
        for _ in range(4):
            page = await service.history(PreferenceHistoryRead(limit=1, cursor=cursor), authority())
            responses.append(page)
            assert page.version == erased.version
            assert len(page.entries) == 1 and page.entries[0].snapshot is None
            revisions.append(page.entries[0].version.revision)
            cursor = page.next_cursor
        assert revisions == [1, 2, 3, 4] and cursor is None
        assert all(sensitive not in response.model_dump_json() for response in responses)
        for table in ("audit_events", "audit_outbox"):
            rows = database.connection.execute(f"SELECT * FROM {table}").fetchall()
            assert sensitive not in repr([tuple(row) for row in rows])
    finally:
        await database.close()
    captured = capsys.readouterr()
    assert sensitive not in captured.out + captured.err
    assert sensitive not in caplog.text
    assert all(sensitive not in repr(record.__dict__) for record in caplog.records)
