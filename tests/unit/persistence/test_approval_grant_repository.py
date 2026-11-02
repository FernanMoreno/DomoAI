import asyncio
import sqlite3
import threading
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from tests.integration.test_durable_approval_store import _t007_issue
from tests.integration.test_household_consent import consent_fixture

from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalGrant


@pytest.mark.parametrize("code", ["consent_revoked", "consent_conflict"])
def test_t008_consent_errors_keep_typed_domain_envelope(code):
    """Break: a consent failure bypasses ErrorCode or changes the shared error envelope."""
    from domoai.domain.errors import DomainError, ErrorCode

    assert code in {item.value for item in ErrorCode}
    error = DomainError(
        ErrorCode(code),
        "Consent rejected",
        field="approval_id",
        device_id="lamp",
        capability="power",
        retryable=True,
        details={"request_id": "r"},
    )
    assert isinstance(error.code, ErrorCode)
    assert error.as_detail().model_dump(mode="json") == {
        "code": code,
        "message": "Consent rejected",
        "field": "approval_id",
        "device_id": "lamp",
        "capability": "power",
        "retryable": True,
        "details": {"request_id": "r"},
    }


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t008_empty_scope_bootstrap_and_missing_head_fail_closed(tmp_path, backend):
    """Break: a GET creates permission, or head loss silently rebuilds old grant authority."""
    from domoai.domain.errors import DomainError
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        repository = ApprovalGrantRepository(fixture.databases[0])
        scope = {"tenant_id": "p04-tenant", "household_id": "p04-home-a"}
        with pytest.raises(DomainError):
            repository.get_consent_control_sync(**scope)
        fixture.databases[0].connection.rollback()
        repository.bootstrap_consent_sync(**scope)
        initial = repository.get_consent_control_sync(**scope)
        assert initial.revision == 0
        assert (
            fixture.databases[0]
            .connection.execute("SELECT COUNT(*) FROM approval_grants")
            .fetchone()[0]
            == 0
        )
        plan, grant = _t007_issue(fixture)
        fixture.databases[0].connection.execute(
            "DELETE FROM consent_control_heads WHERE tenant_id=? AND household_id=?",
            tuple(scope.values()),
        )
        fixture.databases[0].connection.commit()
        with pytest.raises(DomainError):
            fixture.stores[1].validate(grant.approval_id, plan)
        fixture.databases[1].connection.rollback()
        with pytest.raises(DomainError):
            _t007_issue(fixture, suffix="head-lost")
        assert (
            fixture.databases[0]
            .connection.execute("SELECT COUNT(*) FROM consent_control_heads")
            .fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t008_reservation_update_fault_cannot_partially_consume_batch(tmp_path, backend):
    """Break: a grant commits despite a suppressed reservation transition."""
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        fixture.stores[0].reserve(grant.approval_id, plan, reservation_id="rowcount")
        connection = fixture.databases[0].connection
        if backend == "sqlite":
            connection.execute(
                "CREATE TRIGGER t008_ignore BEFORE UPDATE ON approval_reservations "
                "WHEN NEW.status='committed' BEGIN SELECT RAISE(IGNORE); END"
            )
        else:
            connection.execute(
                "CREATE FUNCTION t008_ignore() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RETURN NULL; END $$"
            )
            connection.execute(
                "CREATE TRIGGER t008_ignore BEFORE UPDATE ON approval_reservations "
                "FOR EACH ROW EXECUTE FUNCTION t008_ignore()"
            )
        connection.commit()
        repository = ApprovalGrantRepository(fixture.databases[0])
        assert (
            repository.commit_reservation_batch_sync(
                [grant.approval_id], reservation_id="rowcount", now=fixture.clock.now()
            )
            is False
        )
        observed = (
            fixture.databases[1]
            .connection.execute(
                "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                (grant.approval_id,),
            )
            .fetchone()
        )
        assert tuple(observed) == ("pending", None)
        assert ApprovalGrantRepository(fixture.databases[1]).reservation_status_sync(
            grant.approval_id
        ) == ("rowcount", "reserved")


@pytest.mark.parametrize("invalid", ["expired-status", "receipt-scope", "generation"])
async def test_t008_durable_authority_rejects_changed_status_or_binding(tmp_path, invalid):
    """Break: cached grant survives a nonpending row or mismatched issuance control."""
    from domoai.domain.errors import DomainError
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, "sqlite") as fixture:
        plan, grant = _t007_issue(fixture)
        assert fixture.stores[1].validate(grant.approval_id, plan) == grant
        connection = fixture.databases[0].connection
        if invalid == "expired-status":
            connection.execute("UPDATE approval_grants SET status='expired'")
        elif invalid == "receipt-scope":
            repository = ApprovalGrantRepository(fixture.databases[0])
            repository.bootstrap_consent_sync(tenant_id="p04-tenant", household_id="p04-home-b")
            other = repository.get_consent_control_sync(
                tenant_id="p04-tenant", household_id="p04-home-b"
            )
            connection.execute(
                "UPDATE consent_issuance_receipts SET household_id=?, generation=?",
                ("p04-home-b", str(other.generation)),
            )
        else:
            connection.execute(
                "UPDATE consent_control_heads SET generation=?",
                ("00000000-0000-0000-0000-000000000001",),
            )
        connection.commit()
        with pytest.raises(DomainError):
            fixture.stores[1].validate(grant.approval_id, plan)
        assert connection.execute("SELECT consumed_at FROM approval_grants").fetchone()[0] is None


@contextmanager
def _t007_fail_after_insert(database, backend, table):
    """Inject a real SQL failure after a product write; no repository/DB substitute."""
    assert table in {
        "consent_revocations",
        "physical_intents",
        "approval_grants",
        "consent_deliveries",
    }
    connection = database.connection
    if backend == "sqlite":
        connection.execute(
            f"CREATE TRIGGER t007_write_fault AFTER INSERT ON {table} "
            "BEGIN SELECT RAISE(ABORT, 't007-write-fault'); END"
        )
    else:
        connection.execute(
            "CREATE FUNCTION t007_write_fault() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 't007-write-fault'; END $$"
        )
        connection.execute(
            f"CREATE TRIGGER t007_write_fault AFTER INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION t007_write_fault()"
        )
    connection.commit()
    try:
        yield
    finally:
        connection.rollback()
        if backend == "sqlite":
            connection.execute("DROP TRIGGER t007_write_fault")
        else:
            connection.execute(f"DROP TRIGGER t007_write_fault ON {table}")
            connection.execute("DROP FUNCTION t007_write_fault()")
        connection.commit()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_a_control_receipt_cas_preserves_consumption_and_other_household(
    tmp_path: Path, backend: str
) -> None:
    """Break: R overwrites consumed_at, loses its receipt, or accepts a stale scoped CAS."""
    from domoai.domain.errors import DomainError
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        plan, grant = _t007_issue(fixture)
        _, other_grant = _t007_issue(fixture, home_index=1)
        _, alternate_grant = _t007_issue(fixture, suffix="same-home-alternate")
        fixture.stores[0].consume(grant.approval_id, plan)
        scope = {"tenant_id": "p04-tenant", "household_id": "p04-home-a"}
        head = first.get_consent_control_sync(**scope)
        other_head = second.get_consent_control_sync(
            tenant_id="p04-tenant", household_id="p04-home-b"
        )
        fixture.databases[1].connection.rollback()
        assert head.revision == 0
        request = {
            **scope,
            "approval_id": grant.approval_id,
            "expected_version": head,
            "principal_id": "resident-a",
            "request_id": "revoke-cas",
            "now": fixture.clock.now(),
        }
        receipt = first.revoke_consent_sync(**request)
        assert receipt.status == "revocation_pending"
        current = second.get_consent_control_sync(**scope)
        assert current.generation == head.generation
        assert current.revision == 1
        assert second.revoke_consent_sync(**request) == receipt
        with pytest.raises(DomainError) as conflict:
            second.revoke_consent_sync(**(request | {"request_id": "stale-cas"}))
        assert conflict.value.code == "consent_conflict"
        from domoai.domain.errors import ErrorCode

        assert isinstance(conflict.value.code, ErrorCode)
        conflict_detail = conflict.value.as_detail().model_dump(mode="json")
        assert conflict_detail == {
            "code": "consent_conflict",
            "message": conflict.value.message,
            "field": None,
            "device_id": None,
            "capability": None,
            "retryable": False,
            "details": {},
        }
        with pytest.raises(DomainError) as substituted:
            second.revoke_consent_sync(**(request | {"approval_id": alternate_grant.approval_id}))
        assert substituted.value.code == "consent_conflict"
        assert (
            second.get_consent_control_sync(tenant_id="p04-tenant", household_id="p04-home-b")
            == other_head
        )
        row = (
            fixture.databases[1]
            .connection.execute(
                "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                (grant.approval_id,),
            )
            .fetchone()
        )
        assert tuple(row) == ("consumed", "2026-09-13T12:00:00+00:00")
        fixture.databases[1].connection.rollback()
        await fixture.databases[1].close()
        await fixture.databases[1].initialize()
        restarted = ApprovalGrantRepository(fixture.databases[1])
        assert restarted.get_consent_control_sync(**scope) == current
        assert restarted.revoke_consent_sync(**request) == receipt


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_a_sql_fault_rolls_back_tombstone_head_and_receipt(
    tmp_path: Path, backend: str
) -> None:
    """Break: failed R leaves a tombstone/head advance/receipt without the other writes."""
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        scope = {"tenant_id": "p04-tenant", "household_id": "p04-home-a"}
        baseline = first.get_consent_control_sync(**scope)
        fixture.databases[0].connection.rollback()
        with _t007_fail_after_insert(fixture.databases[0], backend, "consent_revocations"):
            with pytest.raises(
                (sqlite3.DatabaseError, psycopg.DatabaseError), match="t007-write-fault"
            ):
                first.revoke_consent_sync(
                    **scope,
                    approval_id=grant.approval_id,
                    expected_version=baseline,
                    principal_id="resident-a",
                    request_id="failed-r",
                    now=fixture.clock.now(),
                )
            assert second.get_consent_control_sync(**scope) == baseline
            assert second.get_consent_revocation_sync(grant.approval_id) is None
            assert second.get_consent_receipt_sync(**scope, request_id="failed-r") is None
            assert second.is_pending_sync(grant.approval_id) is True
            fixture.databases[1].connection.rollback()
        assert fixture.stores[1].validate(grant.approval_id, plan) == grant


async def _t007_real_preferences(fixture, count=2, *, future_target=None, future_seconds=60):
    """Seed the existing P02 owner, including another household and a future restriction."""
    from tests.fixtures.household_preferences import preference

    from domoai.domain.household_preferences import PreferenceMutation
    from domoai.persistence.household_preferences import HouseholdPreferenceRepository

    own_slots = []
    own_repository = own_version = None
    for household, total in (("p04-home-a", count), ("p04-home-b", 1)):
        repository = HouseholdPreferenceRepository(
            fixture.databases[0], tenant_id="p04-tenant", household_id=household
        )
        await repository.bootstrap()
        version, _ = await repository.snapshot()
        for index in range(total):
            future = index == total - 1
            result = await repository.mutate(
                operation="set",
                request=PreferenceMutation.model_validate(
                    {
                        "expected_version": version.model_dump(mode="json"),
                        "idempotency_key": f"limit-{index}",
                        "preference": preference(
                            target={
                                "kind": "device",
                                "id": future_target
                                if future and future_target is not None
                                else f"{household}-limit-{index}",
                            },
                            valid_from=(
                                fixture.clock.now() + timedelta(seconds=future_seconds)
                            ).isoformat()
                            if future
                            else "2026-09-13T12:00:00+00:00",
                            valid_until="2026-09-13T12:02:00+00:00",
                            timezone="UTC",
                        ),
                    }
                ),
                principal_id="resident-a" if household == "p04-home-a" else "resident-b",
                now=fixture.clock.now(),
            )
            version = result.version
            if household == "p04-home-a":
                own_slots.append(result.preference)
        if household == "p04-home-a":
            own_repository, own_version = repository, version
    return own_repository, own_version, own_slots


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("missing_head", [False, True], ids=["snapshot", "head-error"])
async def test_t007_b_real_preference_helper_preserves_outer_writes_and_rollback(
    tmp_path: Path, backend: str, missing_head: bool
) -> None:
    """Break: nested P02 snapshot commits/rolls back its caller or loses future restrictions."""
    from domoai.domain.errors import DomainError

    async with consent_fixture(tmp_path, backend) as fixture:
        first, second = fixture.databases
        preferences, baseline_version, expected_slots = await _t007_real_preferences(fixture, 101)
        # Bind repository to DB 1, but inject caller DB 2. Ignoring the connection
        # parameter would miss caller writes and read the baseline revision instead.
        first, second = second, first
        connection = first.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            # The helper is read-only: the caller owns this real head lock and tx.
            lock_suffix = " FOR UPDATE" if backend == "postgres" else ""
            connection.execute(
                "SELECT generation, revision FROM household_preference_heads "
                "WHERE tenant_id=? AND household_id=?" + lock_suffix,
                preferences.scope,
            ).fetchone()
            connection.execute(
                "UPDATE household_preference_heads SET revision=102 "
                "WHERE tenant_id=? AND household_id=?",
                preferences.scope,
            )
            if missing_head:
                # Real SQL removes the head in this caller's uncommitted transaction.
                connection.execute(
                    "DELETE FROM household_preference_heads WHERE tenant_id=? AND household_id=?",
                    preferences.scope,
                )
                with pytest.raises(DomainError) as unavailable:
                    preferences.snapshot_in_transaction(connection)
                assert unavailable.value.code == "source_unavailable"
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM household_preference_heads "
                        "WHERE tenant_id=? AND household_id=?",
                        preferences.scope,
                    ).fetchone()[0]
                    == 0
                )
            else:
                version, slots = preferences.snapshot_in_transaction(connection)
                assert version.generation == baseline_version.generation
                assert version.revision == 102
                assert len(slots) == 101
                assert {slot.target.id for slot in slots} == {
                    f"p04-home-a-limit-{index}" for index in range(101)
                }
                assert {slot.preference_id for slot in slots} == {
                    slot.preference_id for slot in expected_slots
                }
                future = next(slot for slot in slots if slot.target.id == "p04-home-a-limit-100")
                assert future.preference.value is False
                assert future.preference.valid_from == datetime(2026, 9, 13, 12, 1, tzinfo=UTC)
                assert (
                    connection.execute(
                        "SELECT revision FROM household_preference_heads "
                        "WHERE tenant_id=? AND household_id=?",
                        preferences.scope,
                    ).fetchone()[0]
                    == 102
                )
            # A helper commit would expose revision 102/deletion; rollback would lose
            # the caller's preceding SQL. Both are observable without mocking DB.
            assert (
                second.connection.execute(
                    "SELECT revision FROM household_preference_heads "
                    "WHERE tenant_id=? AND household_id=?",
                    preferences.scope,
                ).fetchone()[0]
                == 101
            )
            second.connection.rollback()
        finally:
            connection.rollback()
        restored, slots = await preferences.snapshot()
        assert restored == baseline_version
        assert {slot.preference_id for slot in slots} == {
            slot.preference_id for slot in expected_slots
        }


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("failure", ["invalid-payload", "sql-fault"])
async def test_t021a_preference_read_error_leaves_transaction_to_caller(
    tmp_path: Path, backend: str, failure: str
) -> None:
    """Break: unreadable restrictions disappear or a read error closes the caller's tx."""
    from pydantic import ValidationError

    async with consent_fixture(tmp_path, backend) as fixture:
        preferences, baseline_version, expected_slots = await _t007_real_preferences(fixture)
        observer, caller = fixture.databases
        connection = caller.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE household_preference_heads SET revision=3 "
                "WHERE tenant_id=? AND household_id=?",
                preferences.scope,
            )
            if failure == "invalid-payload":
                connection.execute(
                    "UPDATE household_preferences SET payload='' "
                    "WHERE tenant_id=? AND household_id=? AND preference_id=?",
                    (*preferences.scope, str(expected_slots[0].preference_id)),
                )
                with pytest.raises(ValidationError):
                    preferences.snapshot_in_transaction(connection)
            else:
                connection.execute(
                    "ALTER TABLE household_preferences RENAME TO t021a_preferences_unavailable"
                )
                with pytest.raises((sqlite3.DatabaseError, psycopg.DatabaseError)):
                    preferences.snapshot_in_transaction(connection)

            if backend == "postgres":
                expected_status = (
                    psycopg.pq.TransactionStatus.INERROR
                    if failure == "sql-fault"
                    else psycopg.pq.TransactionStatus.INTRANS
                )
                assert connection.info.transaction_status == expected_status
            else:
                assert connection.in_transaction
            if backend == "sqlite" or failure == "invalid-payload":
                assert (
                    connection.execute(
                        "SELECT revision FROM household_preference_heads "
                        "WHERE tenant_id=? AND household_id=?",
                        preferences.scope,
                    ).fetchone()[0]
                    == 3
                )
            assert (
                observer.connection.execute(
                    "SELECT revision FROM household_preference_heads "
                    "WHERE tenant_id=? AND household_id=?",
                    preferences.scope,
                ).fetchone()[0]
                == 2
            )
            observer.connection.rollback()
        finally:
            connection.rollback()
        restored_version, restored_slots = await preferences.snapshot()
        assert restored_version == baseline_version
        assert {slot.preference_id: slot for slot in restored_slots} == {
            slot.preference_id: slot for slot in expected_slots
        }


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_delivery_storage_is_provisioned_and_in_copy_catalog(tmp_path, backend):
    """Break: prepare's receipt/permit row is absent from bootstrap or recovery's catalog."""
    from domoai.persistence.postgres_migration import _TABLES

    async with consent_fixture(tmp_path, backend) as fixture:
        assert (
            fixture.databases[0]
            .connection.execute("SELECT COUNT(*) FROM consent_deliveries")
            .fetchone()[0]
            == 0
        )
        assert "consent_deliveries" in {entry[0] for entry in _TABLES}


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("fault", [False, True], ids=["caller-rollback", "receipt-sql-fault"])
async def test_t007_b_prepare_uses_injected_connection_through_final_receipt(
    tmp_path, backend, fault
):
    """Break: prepare uses its bound DB or closes caller SQL on success/final receipt failure."""
    from tests.integration.test_durable_approval_store import _t007_prepare_request

    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        await fixture.homes[0].plans.save(plan)
        request = await _t007_prepare_request(fixture, plan, grant)
        repository = ApprovalGrantRepository(fixture.databases[0])
        caller = fixture.databases[1].connection
        observer = fixture.databases[0].connection
        injection = (
            _t007_fail_after_insert(fixture.databases[1], backend, "consent_deliveries")
            if fault
            else nullcontext()
        )
        with injection:
            caller.execute("BEGIN IMMEDIATE")
            try:
                caller.execute(
                    "UPDATE plans SET updated_at=? WHERE id=?",
                    ("2026-09-13T12:00:01+00:00", plan.id),
                )
                if fault:
                    with pytest.raises(
                        (sqlite3.DatabaseError, psycopg.DatabaseError), match="t007-write-fault"
                    ):
                        repository.prepare_delivery_sync(
                            connection=caller, now=fixture.clock.now(), **request
                        )
                else:
                    receipt = repository.prepare_delivery_sync(
                        connection=caller, now=fixture.clock.now(), **request
                    )
                    assert receipt.status == "prepared"
                    assert (
                        caller.execute("SELECT COUNT(*) FROM consent_deliveries").fetchone()[0] == 1
                    )
                if backend == "postgres":
                    assert caller.info.transaction_status == (
                        psycopg.pq.TransactionStatus.INERROR
                        if fault
                        else psycopg.pq.TransactionStatus.INTRANS
                    )
                else:
                    assert caller.in_transaction
                assert repository.is_pending_sync(grant.approval_id)
                assert observer.execute("SELECT COUNT(*) FROM physical_intents").fetchone()[0] == 0
                assert (
                    observer.execute("SELECT COUNT(*) FROM consent_deliveries").fetchone()[0] == 0
                )
                assert (
                    observer.execute(
                        "SELECT updated_at FROM plans WHERE id=?", (plan.id,)
                    ).fetchone()[0]
                    == "2026-09-13T12:00:00+00:00"
                )
            finally:
                observer.rollback()
                caller.rollback()
        assert repository.is_pending_sync(grant.approval_id)
        assert observer.execute("SELECT COUNT(*) FROM physical_intents").fetchone()[0] == 0
        assert observer.execute("SELECT COUNT(*) FROM consent_deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_suppressed_receipt_insert_rolls_back_all_authority(tmp_path, backend):
    """Break: a skipped receipt insert returns success and commits an orphaned intent/grant."""
    from tests.integration.test_durable_approval_store import _t007_prepare_request

    from domoai.domain.errors import DomainError
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        request = await _t007_prepare_request(fixture, plan, grant)
        connection = fixture.databases[0].connection
        if backend == "sqlite":
            connection.execute(
                "CREATE TRIGGER t007_ignore BEFORE INSERT ON consent_deliveries "
                "BEGIN SELECT RAISE(IGNORE); END"
            )
        else:
            connection.execute(
                "CREATE FUNCTION t007_ignore() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RETURN NULL; END $$"
            )
            connection.execute(
                "CREATE TRIGGER t007_ignore BEFORE INSERT ON consent_deliveries "
                "FOR EACH ROW EXECUTE FUNCTION t007_ignore()"
            )
        connection.commit()
        with pytest.raises(DomainError) as conflict:
            fixture.stores[0].prepare_delivery(**request)
        assert conflict.value.code == "consent_conflict"
        observer = fixture.databases[1].connection
        assert ApprovalGrantRepository(fixture.databases[1]).is_pending_sync(grant.approval_id)
        assert observer.execute("SELECT COUNT(*) FROM physical_intents").fetchone()[0] == 0
        assert observer.execute("SELECT COUNT(*) FROM consent_deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_b_existing_claim_wrapper_validates_before_transaction(tmp_path, backend):
    """Break: extracting the SQL helper rolls back caller work on the old pre-BEGIN error path."""
    from pydantic import ValidationError
    from tests.integration.test_durable_approval_store import _t007_prepare_request

    from domoai.persistence.coordination import PhysicalIntentRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        await fixture.homes[0].plans.save(plan)
        request = await _t007_prepare_request(fixture, plan, grant)
        connection = fixture.databases[0].connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "UPDATE plans SET updated_at=? WHERE id=?", ("2026-09-13T12:00:01+00:00", plan.id)
            )
            with pytest.raises(ValidationError):
                await PhysicalIntentRepository(fixture.databases[0]).claim_for_dispatch(
                    request["intent"].model_copy(update={"owner_id": None})
                )
            assert (
                connection.execute(
                    "SELECT updated_at FROM plans WHERE id=?", (plan.id,)
                ).fetchone()[0]
                == "2026-09-13T12:00:01+00:00"
            )
        finally:
            connection.rollback()


async def test_t007_b_delivery_schema_upgrades_real_025_and_reopens(tmp_path):
    """Break: completed 026 upgrade omits delivery storage or destroys consumed evidence."""
    from tests.unit.persistence.test_schema_migrations import _t008_seed_real_025

    path = tmp_path / "delivery-upgrade.sqlite"
    grant = _t008_seed_real_025(path)
    database = SQLiteDatabase(path)
    for _ in range(2):
        await database.initialize()
        try:
            assert (
                database.connection.execute("SELECT COUNT(*) FROM consent_deliveries").fetchone()[0]
                == 0
            )
            assert tuple(
                database.connection.execute(
                    "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                    (grant.approval_id,),
                ).fetchone()
            ) == ("consumed", "2026-09-13T12:00:00+00:00")
            assert (
                database.connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE filename='026_household_consent.sql'"
                ).fetchone()[0]
                == 1
            )
            assert (
                database.connection.execute(
                    "SELECT minimum_reader FROM runtime_contract_floor"
                ).fetchone()[0]
                == 4
            )
        finally:
            await database.close()


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("authorization", ["pending", "reserved"])
@pytest.mark.parametrize("finish", ["commit", "rollback", "sql-fault"])
async def test_t007_b_prepare_helper_leaves_atomic_grant_intent_permit_receipt_to_caller(
    tmp_path: Path, backend: str, authorization: str, finish: str
) -> None:
    """Break: nested prepare closes its caller's transaction or leaves partial authority."""
    from tests.integration.test_durable_approval_store import (
        _t007_assert_permit,
        _t007_assert_stored_intent,
        _t007_prepare_request,
    )

    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        await fixture.homes[0].plans.save(plan)
        request = await _t007_prepare_request(fixture, plan, grant)
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        prepare = first.prepare_delivery_sync
        if authorization == "reserved":
            fixture.stores[0].reserve(grant.approval_id, plan, reservation_id="prepare-held")
            request["reservation_id"] = "prepare-held"
        connection = fixture.databases[0].connection
        fault = (
            _t007_fail_after_insert(fixture.databases[0], backend, "physical_intents")
            if finish == "sql-fault"
            else nullcontext()
        )
        with fault:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE plans SET updated_at=? WHERE id=?",
                    ("2026-09-13T12:00:01+00:00", plan.id),
                )
                if finish == "sql-fault":
                    with pytest.raises(
                        (sqlite3.DatabaseError, psycopg.DatabaseError), match="t007-write-fault"
                    ):
                        prepare(connection=connection, now=fixture.clock.now(), **request)
                    if backend == "postgres":
                        assert (
                            connection.info.transaction_status
                            == psycopg.pq.TransactionStatus.INERROR
                        )
                    else:
                        assert connection.in_transaction is True
                else:
                    receipt = prepare(connection=connection, now=fixture.clock.now(), **request)
                    _t007_assert_permit(receipt, request, grant)
                    _t007_assert_stored_intent(connection, receipt)
                    assert (
                        connection.execute(
                            "SELECT status FROM approval_grants WHERE approval_id=?",
                            (grant.approval_id,),
                        ).fetchone()[0]
                        == "consumed"
                    )
                    assert (
                        connection.execute("SELECT COUNT(*) FROM physical_intents").fetchone()[0]
                        == 1
                    )
                    assert (
                        connection.execute(
                            "SELECT updated_at FROM plans WHERE id=?", (plan.id,)
                        ).fetchone()[0]
                        == "2026-09-13T12:00:01+00:00"
                    )
                    if authorization == "reserved":
                        assert first.reservation_status_sync(grant.approval_id) == (
                            "prepare-held",
                            "committed",
                        )
                # The second connection cannot see any uncommitted part, including caller SQL.
                assert second.is_pending_sync(grant.approval_id) is True
                assert (
                    second.get_delivery_receipt_sync(
                        request_id="prepare-a", household_id="p04-home-a"
                    )
                    is None
                )
                assert (
                    second.list_delivery_permits_sync(
                        tenant_id="p04-tenant", household_id="p04-home-a"
                    )
                    == []
                )
                assert (
                    fixture.databases[1]
                    .connection.execute("SELECT COUNT(*) FROM physical_intents")
                    .fetchone()[0]
                    == 0
                )
                assert (
                    fixture.databases[1]
                    .connection.execute("SELECT updated_at FROM plans WHERE id=?", (plan.id,))
                    .fetchone()[0]
                    == "2026-09-13T12:00:00+00:00"
                )
                fixture.databases[1].connection.rollback()
                if finish == "commit":
                    connection.commit()
                else:
                    connection.rollback()
                if finish == "commit":
                    assert (
                        second.get_delivery_receipt_sync(
                            request_id="prepare-a", household_id="p04-home-a"
                        )
                        == receipt
                    )
                    assert second.list_delivery_permits_sync(
                        tenant_id="p04-tenant", household_id="p04-home-a"
                    ) == [receipt.permit]
                    assert second.is_consumed_sync(grant.approval_id) is True
                    _t007_assert_stored_intent(fixture.databases[1].connection, receipt)
                    assert (
                        fixture.databases[1]
                        .connection.execute("SELECT COUNT(*) FROM physical_intents")
                        .fetchone()[0]
                        == 1
                    )
                else:
                    assert (
                        second.get_delivery_receipt_sync(
                            request_id="prepare-a", household_id="p04-home-a"
                        )
                        is None
                    )
                    assert (
                        second.list_delivery_permits_sync(
                            tenant_id="p04-tenant", household_id="p04-home-a"
                        )
                        == []
                    )
                    assert second.is_pending_sync(grant.approval_id) is True
                    assert (
                        fixture.databases[1]
                        .connection.execute("SELECT COUNT(*) FROM physical_intents")
                        .fetchone()[0]
                        == 0
                    )
                    assert second.reservation_status_sync(grant.approval_id) == (
                        ("prepare-held", "reserved") if authorization == "reserved" else None
                    )
                fixture.databases[1].connection.rollback()
            finally:
                connection.rollback()
        if finish == "commit":
            await fixture.databases[1].close()
            await fixture.databases[1].initialize()
            reopened = ApprovalGrantRepository(fixture.databases[1])
            reopened_receipt = reopened.get_delivery_receipt_sync(
                request_id="prepare-a", household_id="p04-home-a"
            )
            assert reopened_receipt == receipt
            _t007_assert_stored_intent(fixture.databases[1].connection, reopened_receipt)


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("invalid_member", ["expired", "foreign-reservation", "mixed-scope"])
async def test_t007_c_batch_rejects_invalid_member_without_partial_consumption(
    tmp_path: Path, backend: str, invalid_member: str
) -> None:
    """Break: batch consumes a valid member despite an expired, re-reserved or foreign-home peer."""
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        first_plan, first_grant = _t007_issue(fixture, suffix="one")
        fixture.clock.set(fixture.clock.now() + timedelta(minutes=1))
        second_plan, second_grant = _t007_issue(
            fixture, suffix="two", home_index=1 if invalid_member == "mixed-scope" else 0
        )
        fixture.stores[0].reserve(first_grant.approval_id, first_plan, reservation_id="batch")
        fixture.stores[0].reserve(second_grant.approval_id, second_plan, reservation_id="batch")
        if invalid_member == "expired":
            # First expires exactly at 12:05; second remains live until 12:06.
            fixture.clock.set(datetime(2026, 9, 13, 12, 5, tzinfo=UTC))
        elif invalid_member == "foreign-reservation":
            assert (
                second.release_reservation_sync(
                    second_grant.approval_id, reservation_id="batch", now=fixture.clock.now()
                )
                is True
            )
            assert (
                second.reserve_if_pending_sync(
                    second_grant.approval_id, reservation_id="other-batch", now=fixture.clock.now()
                )
                is True
            )
        ids = sorted([first_grant.approval_id, second_grant.approval_id], reverse=True)
        assert (
            first.commit_reservation_batch_sync(
                ids, reservation_id="batch", now=fixture.clock.now()
            )
            is False
        )
        for grant in (first_grant, second_grant):
            row = (
                fixture.databases[1]
                .connection.execute(
                    "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                    (grant.approval_id,),
                )
                .fetchone()
            )
            assert tuple(row) == ("pending", None)
            reservation = second.reservation_status_sync(grant.approval_id)
            assert reservation[1] == "reserved"


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("already_committed", [False, True], ids=["reserved", "committed"])
async def test_t007_c_revoked_member_including_committed_never_allows_partial_batch(
    tmp_path: Path, backend: str, already_committed: bool
) -> None:
    """Break: batch skips a revoked committed member and spends an otherwise fresh peer."""
    from domoai.domain.errors import DomainError
    from domoai.persistence.repositories import ApprovalGrantRepository
    from domoai.runtime.approval_store import ApprovalStore

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, revoked = _t007_issue(fixture, suffix="revoked-member")
        fresh_plan, fresh = _t007_issue(fixture, suffix="fresh-member")
        fixture.stores[0].reserve(revoked.approval_id, plan, reservation_id="revoked-batch")
        if already_committed:
            fixture.stores[0].commit_reservation("revoked-batch", [revoked.approval_id])
        fixture.stores[0].reserve(fresh.approval_id, fresh_plan, reservation_id="revoked-batch")
        receipt = fixture.stores[1].revoke_consent(
            revoked.approval_id, authority=fixture.homes[0].resident, request_id="batch-r"
        )
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        assert (
            first.commit_reservation_batch_sync(
                [fresh.approval_id, revoked.approval_id],
                reservation_id="revoked-batch",
                now=fixture.clock.now(),
            )
            is False
        )
        original = (
            fixture.databases[1]
            .connection.execute(
                "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                (revoked.approval_id,),
            )
            .fetchone()
        )
        assert tuple(original) == (
            ("consumed", "2026-09-13T12:00:00+00:00") if already_committed else ("pending", None)
        )
        assert tuple(
            fixture.databases[1]
            .connection.execute(
                "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                (fresh.approval_id,),
            )
            .fetchone()
        ) == ("pending", None)
        assert second.reservation_status_sync(fresh.approval_id) == ("revoked-batch", "reserved")
        fixture.databases[1].connection.rollback()
        fixture.stores[0].release_reservation(
            "revoked-batch", [revoked.approval_id, fresh.approval_id]
        )
        await fixture.databases[0].close()
        await fixture.databases[0].initialize()
        reopened = ApprovalGrantRepository(fixture.databases[0])
        assert reopened.get_consent_revocation_sync(revoked.approval_id) == receipt
        restarted = ApprovalStore(clock=fixture.clock, persistence=reopened)
        with pytest.raises(DomainError) as rejected:
            restarted.validate(revoked.approval_id, plan)
        assert rejected.value.code == "consent_revoked"
        from domoai.domain.errors import ErrorCode

        assert isinstance(rejected.value.code, ErrorCode)
        assert rejected.value.as_detail().model_dump(mode="json") == {
            "code": "consent_revoked",
            "message": rejected.value.message,
            "field": None,
            "device_id": None,
            "capability": None,
            "retryable": False,
            "details": {},
        }
        assert restarted.validate(fresh.approval_id, fresh_plan) == fresh


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("contender", ["reverse-commit", "release-rereserve"])
async def test_t007_c_two_connection_batch_contenders_are_serializable_without_deadlock(
    tmp_path: Path, backend: str, contender: str
) -> None:
    """Break: opposite batch lock order or release/re-reserve produces a nonserializable result."""
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan_a, grant_a = _t007_issue(fixture, suffix="lock-a")
        plan_b, grant_b = _t007_issue(fixture, suffix="lock-b")
        fixture.stores[0].reserve(grant_a.approval_id, plan_a, reservation_id="racing-batch")
        fixture.stores[0].reserve(grant_b.approval_id, plan_b, reservation_id="racing-batch")
        first, second = (ApprovalGrantRepository(db) for db in fixture.databases)
        ids = sorted([grant_a.approval_id, grant_b.approval_id])
        for database in fixture.databases:
            database.connection.rollback()
            if backend == "postgres":
                database.connection.execute("SET lock_timeout = '4s'")
                database.connection.execute("SET statement_timeout = '8s'")
                database.connection.commit()
        before_heads = threading.Barrier(2, timeout=10)

        def commit():
            before_heads.wait()
            return first.commit_reservation_batch_sync(
                ids, reservation_id="racing-batch", now=fixture.clock.now()
            )

        def compete():
            before_heads.wait()
            if contender == "reverse-commit":
                return second.commit_reservation_batch_sync(
                    list(reversed(ids)), reservation_id="racing-batch", now=fixture.clock.now()
                )
            released = second.release_reservation_sync(
                grant_b.approval_id, reservation_id="racing-batch", now=fixture.clock.now()
            )
            reserved = second.reserve_if_pending_sync(
                grant_b.approval_id, reservation_id="r2", now=fixture.clock.now()
            )
            return released, reserved

        outcomes = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(commit), asyncio.to_thread(compete), return_exceptions=True
            ),
            20,
        )
        assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
        if contender == "reverse-commit":
            assert outcomes == [True, True]  # Serial retry observes the same committed batch.
        else:
            assert outcomes in ([True, (False, False)], [False, (True, True)])
        fixture.databases[0].connection.rollback()
        states = [
            tuple(
                fixture.databases[0]
                .connection.execute(
                    "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                    (approval_id,),
                )
                .fetchone()
            )
            for approval_id in ids
        ]
        if outcomes[0]:
            assert states == [("consumed", "2026-09-13T12:00:00+00:00")] * 2
        else:
            assert states == [("pending", None), ("pending", None)]
            assert first.reservation_status_sync(grant_b.approval_id) == ("r2", "reserved")


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t007_c_batch_retry_restart_and_release_preserve_original_consumed_at(
    tmp_path: Path, backend: str
) -> None:
    """Break: idempotent batch retry rewrites consumed_at, or release makes it spendable again."""
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with consent_fixture(tmp_path, backend) as fixture:
        plan, grant = _t007_issue(fixture)
        fixture.stores[0].reserve(grant.approval_id, plan, reservation_id="batch")
        fixture.stores[0].commit_reservation("batch", [grant.approval_id])
        fixture.clock.set(fixture.clock.now() + timedelta(minutes=1))
        await fixture.databases[1].close()
        await fixture.databases[1].initialize()
        restarted = ApprovalGrantRepository(fixture.databases[1])
        assert (
            restarted.commit_reservation_batch_sync(
                [grant.approval_id], reservation_id="batch", now=fixture.clock.now()
            )
            is True
        )
        assert (
            restarted.release_reservation_sync(
                grant.approval_id, reservation_id="batch", now=fixture.clock.now()
            )
            is False
        )
        assert (
            restarted.consume_if_pending_sync(grant.approval_id, now=fixture.clock.now()) is False
        )
        row = (
            fixture.databases[0]
            .connection.execute(
                "SELECT status, consumed_at FROM approval_grants WHERE approval_id=?",
                (grant.approval_id,),
            )
            .fetchone()
        )
        assert tuple(row) == ("consumed", "2026-09-13T12:00:00+00:00")


def _grant() -> ApprovalGrant:
    now = datetime.now(UTC)
    return ApprovalGrant(
        approval_id="approval-1",
        plan_id="plan-1",
        validation_digest="sha256:validation",
        approved_by="operator-1",
        issued_at=now,
        authentication_context="operator-ui",
        session_id="session-1",
        bundle_digest="sha256:bundle",
        recurrence_digest=None,
        validation_valid_until=now + timedelta(minutes=10),
        window_digest="sha256:window",
        schedule_revision=4,
        assertion_nonce="nonce-1",
        approved_at=now,
        expires_at=now + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_approval_grant_survives_restart_with_scope_intact(tmp_path: Path) -> None:
    from domoai.persistence.repositories import ApprovalGrantRepository

    path = tmp_path / "authority.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    repository = ApprovalGrantRepository(database)
    grant = _grant()
    await repository.save(grant)
    await database.close()

    restarted_database = SQLiteDatabase(path)
    await restarted_database.initialize()
    restored = await ApprovalGrantRepository(restarted_database).get(grant.approval_id)

    assert restored == grant
    await restarted_database.close()


@pytest.mark.asyncio
async def test_approval_grant_consumption_is_atomic_and_one_shot(tmp_path: Path) -> None:
    from domoai.persistence.repositories import ApprovalGrantRepository

    database = SQLiteDatabase(tmp_path / "authority.sqlite3")
    await database.initialize()
    repository = ApprovalGrantRepository(database)
    grant = _grant()
    await repository.save(grant)
    now = datetime.now(UTC)

    assert await repository.consume_if_pending(grant.approval_id, now=now) is True
    assert await repository.consume_if_pending(grant.approval_id, now=now) is False
    assert repository.is_consumed_sync(grant.approval_id) is True


@pytest.mark.asyncio
async def test_consumed_approval_status_survives_repository_restart(tmp_path: Path) -> None:
    from domoai.persistence.repositories import ApprovalGrantRepository

    path = tmp_path / "authority-restart.sqlite3"
    database = SQLiteDatabase(path)
    await database.initialize()
    grant = _grant()
    repository = ApprovalGrantRepository(database)
    await repository.save(grant)
    assert await repository.consume_if_pending(grant.approval_id, now=datetime.now(UTC)) is True
    await database.close()

    restarted = SQLiteDatabase(path)
    await restarted.initialize()
    restored_repository = ApprovalGrantRepository(restarted)

    assert restored_repository.is_consumed_sync(grant.approval_id) is True
    assert await restored_repository.get(grant.approval_id) == grant
    await restarted.close()

    await database.close()


# --- T011-C5 RED: durable decision receipt written with the grant (freeze p04-t011-c5 + R1) ---


def _c5_record(fixture, request_id="req-1", **changes):
    from domoai.runtime.approval_store import ConsentDecisionRecord

    values = {
        "principal_id": fixture.homes[0].resident_principal.id,
        "request_id": request_id,
        "fingerprint": "f" * 64,
        "review_digest": "sha256:" + "d" * 64,
        "reviewed_control_version": {
            "generation": "00000000-0000-4000-8000-000000000004",
            "revision": 3,
        },
    }
    values.update(changes)
    return ConsentDecisionRecord(**values)


def _c5_issue(fixture, suffix, decision, *, home_index=0, store_index=0):
    from tests.integration.test_durable_approval_store import _plan

    from domoai.runtime.approval_store import ApprovalAssertion

    home = fixture.homes[home_index]
    now = fixture.clock.now()
    from domoai.domain.models import ValidationResult, ValidationStatus

    plan = _plan().model_copy(
        update={
            "id": f"{home.agent.household_id}:{suffix}",
            "authority": home.agent,
            "commands": [home.command().model_copy(update={"id": suffix})],
            "validation": ValidationResult(
                status=ValidationStatus.REQUIRES_CONFIRMATION,
                validated_at=now,
                runtime_revision="revision-1",
                digest="sha256:" + "a" * 64,
                valid_until=None,
            ),
        }
    )
    grant = fixture.stores[store_index].issue_authenticated(
        plan,
        principal=home.resident_principal,
        assertion=ApprovalAssertion(
            principal=home.resident_principal,
            nonce=f"{plan.id}:nonce",
            approved_at=now,
            expires_at=now + timedelta(minutes=5),
            plan_id=plan.id,
            validation_digest=plan.validation.digest,
        ),
        decision=decision,
    )
    return plan, grant


def _c5_count(fixture, table, db_index=0):
    connection = fixture.databases[db_index].connection
    value = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    connection.rollback()
    return value


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c5_decision_receipt_is_written_with_the_grant(tmp_path, backend):
    """Break: the receipt is missing, partial, or readable by another identity."""
    async with consent_fixture(tmp_path, backend) as fixture:
        record = _c5_record(fixture)
        _, grant = _c5_issue(fixture, "approved", record)
        scope = {"tenant_id": "p04-tenant", "household_id": "p04-home-a"}
        repository = fixture.stores[0]._persistence
        found = repository.get_decision_receipt_sync(
            **scope, principal_id=record.principal_id, request_id="req-1"
        )
        assert found is not None
        fingerprint, response = found
        assert fingerprint == "f" * 64
        assert response == {
            "kind": "approval",
            "approval_id": grant.approval_id,
            "expires_at": grant.expires_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "review_digest": record.review_digest,
            "reviewed_control_version": record.reviewed_control_version,
        }
        assert _c5_count(fixture, "consent_receipts") == 1
        for other in (
            {**scope, "principal_id": "someone-else", "request_id": "req-1"},
            {**scope, "principal_id": record.principal_id, "request_id": "req-2"},
            {
                "tenant_id": "p04-tenant",
                "household_id": "p04-home-b",
                "principal_id": record.principal_id,
                "request_id": "req-1",
            },
        ):
            assert repository.get_decision_receipt_sync(**other) is None
        fixture.databases[0].connection.rollback()
        # Another connection sees the committed receipt, and no secret fields leaked into it.
        row = (
            fixture.databases[1]
            .connection.execute("SELECT response FROM consent_receipts")
            .fetchone()
        )
        fixture.databases[1].connection.rollback()
        import json

        assert set(json.loads(row[0])) == {
            "kind",
            "approval_id",
            "expires_at",
            "review_digest",
            "reviewed_control_version",
        }


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c5_duplicate_request_rolls_back_grant_and_issuance_receipt(tmp_path, backend):
    """Break: a lost PK race leaves a second grant or issuance receipt behind."""
    from domoai.domain.errors import DomainError, ErrorCode

    async with consent_fixture(tmp_path, backend) as fixture:
        _c5_issue(fixture, "first", _c5_record(fixture))
        before = (
            _c5_count(fixture, "approval_grants"),
            _c5_count(fixture, "consent_issuance_receipts"),
            _c5_count(fixture, "consent_receipts"),
        )
        with pytest.raises(DomainError) as caught:
            _c5_issue(fixture, "second", _c5_record(fixture, fingerprint="e" * 64))
        assert caught.value.code == ErrorCode.CONSENT_CONFLICT
        fixture.databases[0].connection.rollback()
        after = (
            _c5_count(fixture, "approval_grants"),
            _c5_count(fixture, "consent_issuance_receipts"),
            _c5_count(fixture, "consent_receipts"),
        )
        assert after == before
        # The connection stays usable after the conflict (no aborted PostgreSQL transaction).
        _c5_issue(fixture, "third", _c5_record(fixture, request_id="req-3"))
        assert _c5_count(fixture, "approval_grants") == before[0] + 1


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("bad", ["principal-mismatch", "empty-request", "long-request"])
async def test_t011_c5_decision_identity_is_validated_before_any_write(tmp_path, backend, bad):
    from domoai.domain.errors import DomainError

    async with consent_fixture(tmp_path, backend) as fixture:
        changes = {
            "principal-mismatch": {"principal_id": "intruder"},
            "empty-request": {"request_id": " "},
            "long-request": {"request_id": "r" * 201},
        }[bad]
        with pytest.raises(DomainError):
            _c5_issue(fixture, "bad", _c5_record(fixture, **changes))
        fixture.databases[0].connection.rollback()
        assert _c5_count(fixture, "approval_grants") == 0
        assert _c5_count(fixture, "consent_receipts") == 0


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c5_approval_and_revocation_receipts_never_cross_parse(tmp_path, backend):
    """Break: an approval row is parsed as a revocation (500) or counted for another principal."""
    from domoai.domain.errors import DomainError, ErrorCode
    from domoai.domain.models import AuthorityContext, PrincipalRole

    async with consent_fixture(tmp_path, backend) as fixture:
        _, first = _c5_issue(fixture, "approved", _c5_record(fixture, request_id="shared"))
        _, second = _c5_issue(fixture, "other", _c5_record(fixture, request_id="req-2"))
        store = fixture.stores[0]
        repository = store._persistence
        scope = {"tenant_id": "p04-tenant", "household_id": "p04-home-a"}
        operator = AuthorityContext(
            **scope,
            household_ids=["p04-home-a"],
            principal_id=fixture.homes[0].resident_principal.id,
            roles=[PrincipalRole.OPERATOR],
        )
        with pytest.raises(DomainError) as reused:
            store.revoke_consent(second.approval_id, authority=operator, request_id="shared")
        assert reused.value.code == ErrorCode.CONSENT_CONFLICT
        fixture.databases[0].connection.rollback()
        with pytest.raises(DomainError) as direct:
            repository.get_consent_receipt_sync(
                **scope, principal_id=operator.principal_id, request_id="shared"
            )
        assert direct.value.code == ErrorCode.CONSENT_CONFLICT
        fixture.databases[0].connection.rollback()
        # A revocation by another principal with the same request id is still readable
        # without a principal: approval rows are filtered before the ambiguity check.
        other = operator.model_copy(update={"principal_id": "operator-x"})
        receipt = store.revoke_consent(first.approval_id, authority=other, request_id="shared")
        assert repository.get_consent_receipt_sync(**scope, request_id="shared") == receipt
        # A revocation row is never a decision receipt.
        assert (
            repository.get_decision_receipt_sync(
                **scope, principal_id="operator-x", request_id="shared"
            )
            is None
        )
        # The approval receipt is historical: it survives its grant's revocation.
        assert (
            repository.get_decision_receipt_sync(
                **scope,
                principal_id=fixture.homes[0].resident_principal.id,
                request_id="shared",
            )
            is not None
        )


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c5_issue_without_a_decision_keeps_the_legacy_call_and_no_receipt(
    tmp_path, backend
):
    async with consent_fixture(tmp_path, backend) as fixture:
        _c5_issue(fixture, "legacy", None)
        assert _c5_count(fixture, "approval_grants") == 1
        assert _c5_count(fixture, "consent_receipts") == 0


async def test_t011_c5_persistence_doubles_with_the_old_save_signature_still_work():
    from tests.integration.test_durable_approval_store import _plan

    from domoai.domain.models import ValidationResult, ValidationStatus
    from domoai.runtime.approval_store import ApprovalAssertion, ApprovalStore, OperatorPrincipal
    from domoai.runtime.clock import FixedClock

    saved = []

    class OldDouble:
        def save_sync(self, grant):
            saved.append(grant)

        def nonce_exists_sync(self, nonce):
            return False

    now = datetime(2026, 10, 27, 10, tzinfo=UTC)
    store = ApprovalStore(clock=FixedClock(now), persistence=OldDouble())
    plan = _plan().model_copy(
        update={
            "validation": ValidationResult(
                status=ValidationStatus.REQUIRES_CONFIRMATION,
                validated_at=now,
                runtime_revision="r",
                digest="sha256:" + "a" * 64,
                valid_until=None,
            )
        }
    )
    principal = OperatorPrincipal("resident", "resident_browser", "s")
    assertion = ApprovalAssertion(
        principal,
        "n",
        now,
        now + timedelta(minutes=5),
        plan_id=plan.id,
        validation_digest=plan.validation.digest,
    )
    store.issue_authenticated(plan, principal=principal, assertion=assertion)
    assert len(saved) == 1


async def test_t011_c5_store_without_persistence_ignores_the_decision_record():
    from tests.integration.test_durable_approval_store import _plan

    from domoai.domain.models import ValidationResult, ValidationStatus
    from domoai.runtime.approval_store import (
        ApprovalAssertion,
        ApprovalStore,
        ConsentDecisionRecord,
        OperatorPrincipal,
    )
    from domoai.runtime.clock import FixedClock

    now = datetime(2026, 10, 27, 10, tzinfo=UTC)
    store = ApprovalStore(clock=FixedClock(now))
    plan = _plan().model_copy(
        update={
            "validation": ValidationResult(
                status=ValidationStatus.REQUIRES_CONFIRMATION,
                validated_at=now,
                runtime_revision="r",
                digest="sha256:" + "a" * 64,
                valid_until=None,
            )
        }
    )
    principal = OperatorPrincipal("resident", "resident_browser", "s")
    assertion = ApprovalAssertion(
        principal,
        "n",
        now,
        now + timedelta(minutes=5),
        plan_id=plan.id,
        validation_digest=plan.validation.digest,
    )
    record = ConsentDecisionRecord("resident", "req", "f" * 64, "sha256:" + "d" * 64, {})
    store.issue_authenticated(plan, principal=principal, assertion=assertion, decision=record)
    assert store.get_decision_receipt("t", "h", "resident", "req") is None


# --- T011-C5b RED: revocation insert is race-safe -------------------------------------------


class _InjectingConnection:
    """Delegates to a real connection; plants a rival receipt right before the revoke insert."""

    def __init__(self, inner, rival):
        self._inner = inner
        self._rival = rival
        self.fired = False

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().startswith("INSERT INTO consent_receipts") and not self.fired:
            self.fired = True
            self._inner.execute(*self._rival)
        return self._inner.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_t011_c5b_revocation_receipt_insert_conflict_is_a_typed_error(tmp_path):
    """Break: a rival receipt winning the key makes revoke fail with a raw IntegrityError."""
    from domoai.domain.errors import DomainError, ErrorCode
    from domoai.domain.models import AuthorityContext, PrincipalRole

    async with consent_fixture(tmp_path, "sqlite") as fixture:
        _, grant = _c5_issue(fixture, "target", _c5_record(fixture, request_id="req-a"))
        database = fixture.databases[0]
        scope = ("p04-tenant", "p04-home-a")
        rival = (
            "INSERT INTO consent_receipts VALUES (?, ?, ?, ?, ?, ?)",
            (*scope, "operator-x", "race", "f" * 64, '{"kind":"approval"}'),
        )
        real = database._connection
        database._connection = _InjectingConnection(real, rival)
        try:
            operator = AuthorityContext(
                tenant_id=scope[0],
                household_id=scope[1],
                household_ids=[scope[1]],
                principal_id="operator-x",
                roles=[PrincipalRole.OPERATOR],
            )
            with pytest.raises(DomainError) as caught:
                fixture.stores[0].revoke_consent(
                    grant.approval_id, authority=operator, request_id="race"
                )
        finally:
            database._connection = real
        assert caught.value.code == ErrorCode.CONSENT_CONFLICT
        real.rollback()
        assert real.execute("SELECT COUNT(*) FROM consent_revocations").fetchone()[0] == 0, (
            "a failed revoke must not leave a tombstone"
        )


# --- T011-C7 RED: active-approval listing and the resident revoke fingerprint ---------------


def _c7_scope(fixture, home_index=0):
    home = fixture.homes[home_index]
    return home.agent.tenant_id, home.agent.household_id


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c7_list_active_approvals_follows_the_freeze_definition(tmp_path, backend):
    """Break: revoked, expired, foreign-household or other-plan grants counted as active."""
    from datetime import timedelta

    async with consent_fixture(tmp_path, backend) as fixture:
        now = fixture.clock.now()
        tenant, household = _c7_scope(fixture)
        repository = fixture.stores[0]._persistence
        plan_a, grant_a = _c5_issue(fixture, "c7-a", None)
        plan_b, grant_b = _c5_issue(fixture, "c7-b", None)
        foreign_plan, _foreign = _c5_issue(fixture, "c7-a", None, home_index=1)
        listed = repository.list_active_approval_ids_sync(tenant, household, plan_a.id, now)
        assert listed == [grant_a.approval_id]
        assert repository.list_active_approval_ids_sync(tenant, household, plan_b.id, now) == [
            grant_b.approval_id
        ]
        assert repository.list_active_approval_ids_sync(tenant, household, "nope", now) == []
        other_tenant, other_household = _c7_scope(fixture, 1)
        assert repository.list_active_approval_ids_sync(
            tenant, household, foreign_plan.id, now
        ) == []
        assert repository.list_active_approval_ids_sync(
            other_tenant, other_household, plan_a.id, now
        ) == []
        # A pending grant is no longer active once its valid_until is not strictly in the future.
        expiry = grant_a.expires_at
        assert repository.list_active_approval_ids_sync(tenant, household, plan_a.id, expiry) == []
        assert repository.list_active_approval_ids_sync(
            tenant, household, plan_a.id, expiry + timedelta(minutes=1)
        ) == []
        # A consumed grant stays active until it is revoked.
        assert repository.consume_if_pending_sync(grant_b.approval_id, now=now) is True
        assert repository.list_active_approval_ids_sync(tenant, household, plan_b.id, now) == [
            grant_b.approval_id
        ]
        # valid_until only bounds pending grants: a consumed one stays active past it.
        assert repository.list_active_approval_ids_sync(
            tenant, household, plan_b.id, grant_b.expires_at + timedelta(minutes=1)
        ) == [grant_b.approval_id]
        fixture.stores[0].revoke_consent(
            grant_a.approval_id, authority=fixture.homes[0].resident, request_id="c7-r"
        )
        fixture.databases[0].connection.rollback()
        assert repository.list_active_approval_ids_sync(tenant, household, plan_a.id, now) == []


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c7_store_lists_active_approvals_and_is_empty_without_persistence(
    tmp_path, backend
):
    from domoai.runtime.approval_store import ApprovalStore

    async with consent_fixture(tmp_path, backend) as fixture:
        tenant, household = _c7_scope(fixture)
        plan, grant = _c5_issue(fixture, "c7-store", None)
        assert fixture.stores[0].list_active_approvals(tenant, household, plan.id) == [
            grant.approval_id
        ]
    assert ApprovalStore().list_active_approvals("t", "h", "p") == []


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c7_revoke_fingerprint_is_unchanged_without_a_request_fingerprint(
    tmp_path, backend
):
    """Break: a None request_fingerprint changes the legacy hash and breaks stored receipts."""
    import hashlib
    import json

    async with consent_fixture(tmp_path, backend) as fixture:
        _, grant = _c5_issue(fixture, "c7-legacy", None)
        scope = _c7_scope(fixture)
        repository = fixture.stores[0]._persistence
        before = repository.get_consent_control_sync(
            tenant_id=scope[0], household_id=scope[1]
        )
        connection = fixture.databases[0].connection
        payload = connection.execute(
            "SELECT payload FROM approval_grants WHERE approval_id=?", (grant.approval_id,)
        ).fetchone()[0]
        connection.rollback()
        fixture.stores[0].revoke_consent(
            grant.approval_id, authority=fixture.homes[0].resident, request_id="legacy"
        )
        stored = connection.execute(
            "SELECT fingerprint FROM consent_receipts WHERE request_id='legacy'"
        ).fetchone()[0]
        connection.rollback()
        legacy = hashlib.sha256(
            json.dumps(
                [grant.approval_id, payload, before.model_dump(mode="json")], sort_keys=True
            ).encode()
        ).hexdigest()
        assert stored == legacy


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c7_request_fingerprint_makes_retries_exact_and_other_bodies_conflict(
    tmp_path, backend
):
    """Break: another body with the same request id gets a stored receipt, or a retry re-revokes."""
    from domoai.domain.errors import DomainError, ErrorCode

    async with consent_fixture(tmp_path, backend) as fixture:
        _, grant = _c5_issue(fixture, "c7-fp", None)
        store, operator = fixture.stores[0], fixture.homes[0].resident
        first = store.revoke_consent(
            grant.approval_id, authority=operator, request_id="r-1", request_fingerprint="a" * 64
        )
        again = store.revoke_consent(
            grant.approval_id, authority=operator, request_id="r-1", request_fingerprint="a" * 64
        )
        assert again == first
        with pytest.raises(DomainError) as caught:
            store.revoke_consent(
                grant.approval_id,
                authority=operator,
                request_id="r-1",
                request_fingerprint="b" * 64,
            )
        assert caught.value.code == ErrorCode.CONSENT_CONFLICT
        with pytest.raises(DomainError) as legacy:
            store.revoke_consent(grant.approval_id, authority=operator, request_id="r-1")
        assert legacy.value.code == ErrorCode.CONSENT_CONFLICT
        assert _c5_count(fixture, "consent_revocations") == 1


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c7h_listing_can_exclude_grants_issued_after_a_moment(tmp_path, backend):
    """Break: a grant issued after the review is listed as revocable."""
    from datetime import timedelta

    async with consent_fixture(tmp_path, backend) as fixture:
        now = fixture.clock.now()
        tenant, household = _c7_scope(fixture)
        repository = fixture.stores[0]._persistence
        plan, grant = _c5_issue(fixture, "c7h-a", None)
        issued = grant.issued_at
        before = issued - timedelta(seconds=1)
        assert repository.list_active_approval_ids_sync(
            tenant, household, plan.id, now, issued_not_after=before
        ) == []
        assert repository.list_active_approval_ids_sync(
            tenant, household, plan.id, now, issued_not_after=issued
        ) == [grant.approval_id]


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_t011_c7h_resolution_uses_the_caller_clock_and_issue_bound(tmp_path, backend):
    """Break: the store clock decides what is pending, not the reviewed instant."""
    from datetime import timedelta

    async with consent_fixture(tmp_path, backend) as fixture:
        tenant, household = _c7_scope(fixture)
        store = fixture.stores[0]
        plan, grant = _c5_issue(fixture, "c7h-b", None)
        assert store.resolve_active_approval(tenant, household, plan.id) == grant.approval_id
        late = grant.expires_at + timedelta(minutes=1)
        assert store.resolve_active_approval(tenant, household, plan.id, now=late) is None
        assert (
            store.resolve_active_approval(
                tenant, household, plan.id, issued_not_after=grant.issued_at - timedelta(seconds=1)
            )
            is None
        )
