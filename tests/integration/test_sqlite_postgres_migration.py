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
            # Restore the P04 floor so the next failure exercises copy rollback.
            source.connection.execute(
                "UPDATE runtime_contract_floor SET minimum_reader=4,"
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
        # Both writers publish the P04 floor while preserving the original checkpoint.
        assert results == [(1, 4, *original[2:])] * 2


# Independent acceptance inventory: never derive expected tables from _TABLES.
_T009_EXPECTED_COUNTS = {
    "runtime_contract_floor": 1,
    "approval_grants": 6,
    "approval_reservations": 2,
    "consent_control_heads": 2,
    "consent_issuance_receipts": 6,
    "consent_receipts": 2,
    "consent_revocations": 2,
    "physical_intents": 3,
    "consent_deliveries": 1,
    "household_preference_heads": 2,
    "household_preferences": 3,
    "household_preference_history": 3,
    "household_preference_requests": 3,
}


def _t009_rows(database):
    return {
        table: [
            tuple(row)
            for row in database.connection.execute(
                f"SELECT * FROM {table} ORDER BY 1, 2"
            ).fetchall()
        ]
        for table in _T009_EXPECTED_COUNTS
    }


@pytest.fixture
async def t009_migration(tmp_path):
    """Quiescent source and disposable PostgreSQL; no skip/fallback or runtime delivery."""
    from domoai.domain.coordination import PhysicalIntentStatus
    from domoai.persistence.coordination import PhysicalIntentRepository
    from tests.fixtures.household_onboarding import connection_databases
    from tests.integration.test_durable_approval_store import _t007_issue, _t007_prepare_request
    from tests.integration.test_household_consent import consent_fixture

    async with consent_fixture(tmp_path / "source", "sqlite") as fixture:
        store = fixture.stores[0]
        grants = {
            state: _t007_issue(fixture, suffix=f"t009-{state}")
            for state in ("pending", "reserved", "consumed", "revoked", "prepared")
        }
        _t007_issue(fixture, home_index=1, suffix="t009-other-home")
        plan, grant = grants["reserved"]
        store.reserve(grant.approval_id, plan, reservation_id="t009-held")
        plan, grant = grants["consumed"]
        store.consume(grant.approval_id, plan)
        plan, grant = grants["prepared"]
        store.reserve(grant.approval_id, plan, reservation_id="t009-committed")
        request = await _t007_prepare_request(fixture, plan, grant)
        request["reservation_id"] = "t009-committed"
        delivery = store.prepare_delivery(**request)
        revocations = [
            store.revoke_consent(
                grants[state][1].approval_id,
                authority=fixture.homes[0].resident,
                request_id=f"t009-revoke-{state}",
            )
            for state in ("revoked", "prepared")
        ]
        assert revocations[0].permit_ids == []
        assert revocations[1].permit_ids == [delivery.permit.permit_id]
        assert all(receipt.status == "revocation_pending" for receipt in revocations)
        source = fixture.databases[0]
        ledger = PhysicalIntentRepository(source, clock=fixture.clock)
        for status in (PhysicalIntentStatus.CONFIRMED, PhysicalIntentStatus.UNKNOWN):
            claim = await ledger.claim_for_dispatch(
                request["intent"].model_copy(
                    update={
                        "idempotency_key": f"t009-ledger-{status.value}",
                        "command_id": f"t009-ledger-{status.value}",
                    }
                )
            )
            await ledger.settle(intent=claim.intent, status=status)
        source.connection.execute(
            "UPDATE runtime_contract_floor SET checkpoint_id='t009-source-checkpoint'"
        )
        source.connection.commit()
        expected = _t009_rows(source)
        assert {table: len(rows) for table, rows in expected.items()} == _T009_EXPECTED_COUNTS
        assert sorted(row[1] for row in expected["consent_control_heads"]) == [
            "p04-home-a",
            "p04-home-b",
        ]
        assert sorted(row[3] for row in expected["physical_intents"]) == [
            "confirmed",
            "dispatching",
            "unknown",
        ]
        assert sorted(row[2] for row in expected["approval_reservations"]) == [
            "committed",
            "reserved",
        ]
        assert sum(row[5] is not None for row in expected["approval_grants"]) == 2
        source_changes = source.connection.total_changes
        async with connection_databases(tmp_path / "destination", "postgres") as databases:
            destination, observer = databases
            yield source, destination, observer, expected, delivery, revocations
        assert _t009_rows(source) == expected
        assert source.connection.total_changes == source_changes


def _t009_assert_copy(report, observer, expected):
    assert {table: report.table_counts[table] for table in expected} == _T009_EXPECTED_COUNTS
    assert _t009_rows(observer) == expected
    assert all(report.payload_digests[table] for table in expected)
    observer.connection.rollback()


async def test_t009_copy_reopen_reupgrade_preserves_consent(t009_migration, monkeypatch):
    """Break: copy/reopen omits or rewrites P04 evidence, or old reader writes past floor4."""
    import psycopg

    from domoai.persistence import contract_compatibility
    from domoai.persistence.repositories import ApprovalGrantRepository

    source, destination, observer, expected, delivery, revocations = t009_migration
    report = await migrate_sqlite_to_postgres(source, destination)
    _t009_assert_copy(report, observer, expected)
    await destination.close()
    with monkeypatch.context() as prior_reader:
        prior_reader.setattr(contract_compatibility, "SUPPORTED_RUNTIME_READER", 3)
        for previous in (SQLiteDatabase(source.path), PostgresDatabase(destination.dsn)):
            with pytest.raises(contract_compatibility.UnsupportedRuntimeContract):
                await previous.initialize()
            await previous.close()
    await destination.initialize()
    await destination.close()
    await destination.open_existing()
    assert _t009_rows(destination) == expected
    repository = ApprovalGrantRepository(destination)
    assert (
        repository.get_delivery_receipt_sync(request_id="prepare-a", household_id="p04-home-a")
        == delivery
    )
    for receipt in revocations:
        assert repository.get_consent_revocation_sync(receipt.approval_id) == receipt
    destination.connection.rollback()
    # Constraints survive the copy and reupgrade; neither probe can alter evidence.
    with pytest.raises(psycopg.errors.UniqueViolation):
        destination.connection.execute(
            "INSERT INTO consent_deliveries SELECT * FROM consent_deliveries"
        )
    destination.connection.rollback()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        destination.connection.execute(
            "INSERT INTO consent_deliveries "
            "SELECT 't009-orphan', 'missing-grant', tenant_id, household_id, "
            "'orphan-request', 'missing-intent', fingerprint, response FROM consent_deliveries"
        )
    destination.connection.rollback()
    _t009_assert_copy(report, observer, expected)


@pytest.mark.parametrize("fault", ["sql", "mismatch"])
async def test_t009_late_copy_failure_rolls_back_and_retries(t009_migration, fault):
    """Break: late delivery failure leaves consumed grants/intents/checkpoint partly copied."""
    import psycopg

    from domoai.persistence.postgres_migration import MigrationVerificationError

    source, destination, observer, expected, _, _ = t009_migration
    baseline = _t009_rows(observer)
    observer.connection.rollback()
    effect = (
        "RAISE EXCEPTION 't009 late delivery failure';"
        if fault == "sql"
        else "NEW.response := NEW.response || ' '; RETURN NEW;"
    )
    destination.connection.execute(
        "CREATE FUNCTION t009_delivery_fault() RETURNS trigger LANGUAGE plpgsql AS $$ "
        "BEGIN "
        "IF (SELECT COUNT(*) FROM approval_grants) <> 6 "
        "OR (SELECT COUNT(*) FROM physical_intents) <> 3 "
        "OR (SELECT COUNT(*) FROM consent_revocations) <> 2 THEN "
        "RAISE EXCEPTION 't009 fixture did not reach late copy'; END IF; " + effect + " END $$"
    )
    destination.connection.execute(
        "CREATE TRIGGER t009_delivery_fault BEFORE INSERT ON consent_deliveries "
        "FOR EACH ROW EXECUTE FUNCTION t009_delivery_fault()"
    )
    destination.connection.commit()
    error = psycopg.errors.RaiseException if fault == "sql" else MigrationVerificationError
    message = "t009 late delivery failure" if fault == "sql" else "consent_deliveries"
    with pytest.raises(error, match=message):
        await migrate_sqlite_to_postgres(source, destination)
    assert _t009_rows(observer) == baseline
    observer.connection.rollback()
    assert _t009_rows(source) == expected
    destination.connection.execute("DROP TRIGGER t009_delivery_fault ON consent_deliveries")
    destination.connection.execute("DROP FUNCTION t009_delivery_fault()")
    destination.connection.commit()
    report = await migrate_sqlite_to_postgres(source, destination)
    _t009_assert_copy(report, observer, expected)


async def test_t009_copy_conflict_preserves_confirmed_destination(t009_migration):
    """Break: migration overwrites a confirmed receipt/checkpoint or commits preceding rows."""
    from domoai.persistence.postgres_migration import MigrationConflict

    source, destination, observer, expected, _, _ = t009_migration
    destination.connection.execute(
        "INSERT INTO consent_receipts VALUES (%s,%s,%s,%s,%s,%s)",
        expected["consent_receipts"][0],
    )
    destination.connection.execute(
        "UPDATE runtime_contract_floor SET checkpoint_id='t009-confirmed-destination'"
    )
    destination.connection.commit()
    baseline = _t009_rows(observer)
    observer.connection.rollback()
    for _ in range(2):
        with pytest.raises(MigrationConflict, match="consent_receipts"):
            await migrate_sqlite_to_postgres(source, destination)
        assert _t009_rows(observer) == baseline
        observer.connection.rollback()
        assert _t009_rows(source) == expected
