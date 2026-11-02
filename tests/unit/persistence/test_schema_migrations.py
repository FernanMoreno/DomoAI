from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.domain.models import Command, Plan
from domoai.persistence.repositories import PlanRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import MIGRATIONS_DIR, SQLiteDatabase


def _t008_seed_real_025(path):
    """Build the old schema/floor directly; never call the current initializer."""
    import json
    import sqlite3
    from dataclasses import asdict

    from tests.unit.persistence.test_approval_grant_repository import _grant

    grant = _grant()
    payload = asdict(grant)
    payload["authority"] = grant.authority.model_dump(mode="json")
    with sqlite3.connect(path) as raw:
        raw.execute(
            "CREATE TABLE schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration.name < "026":
                raw.executescript(migration.read_text(encoding="utf-8"))
                raw.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?)",
                    (migration.name, "2026-09-13T12:00:00+00:00"),
                )
        # Match the preceding reader's bootstrap, preserving checkpoint evidence.
        raw.execute("UPDATE runtime_contract_floor SET minimum_reader=3")
        raw.execute(
            "INSERT INTO approval_grants VALUES (?, ?, ?, ?, 'consumed', ?)",
            (
                grant.approval_id,
                json.dumps(payload, default=str),
                grant.issued_at.isoformat(),
                grant.expires_at.isoformat(),
                "2026-09-13T12:00:00+00:00",
            ),
        )
        assert raw.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[0] == 3
        assert raw.execute("SELECT MAX(filename) FROM schema_migrations").fetchone()[0] == (
            "025_household_preferences.sql"
        )
        assert (
            raw.execute("SELECT name FROM sqlite_master WHERE name LIKE 'consent_%'").fetchall()
            == []
        )
    return grant


@pytest.mark.asyncio
async def test_t008_interrupted_026_rejects_old_writer_at_durable_commit(tmp_path):
    """Break: committed 026 still admits reader3, which can create an untracked grant."""
    import json
    import sqlite3

    from domoai.persistence import contract_compatibility
    from domoai.persistence.repositories import ApprovalGrantRepository

    path = tmp_path / "interrupted-026.sqlite"
    grant = _t008_seed_real_025(path)
    with sqlite3.connect(path) as raw:
        baseline = raw.execute("SELECT * FROM approval_grants").fetchall()
        floor = raw.execute("SELECT * FROM runtime_contract_floor").fetchone()
        assert floor[1] == 3
    real_connect = sqlite3.connect

    class StopAfterDurable026(sqlite3.Connection):
        stopped = False

        def commit(self):
            super().commit()
            if (
                not self.stopped
                and self.execute(
                    "SELECT 1 FROM schema_migrations WHERE filename='026_household_consent.sql'"
                ).fetchone()
                is not None
            ):
                self.stopped = True
                raise RuntimeError("t008-stop-immediately-after-durable-026")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=StopAfterDurable026)

    database = SQLiteDatabase(path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(RuntimeError, match="t008-stop-immediately-after-durable-026"):
            await database.initialize()
    # A separate ordinary connection sees the actual durable commit, not a mock.
    with sqlite3.connect(path) as old_writer:
        assert (
            old_writer.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE filename='026_household_consent.sql'"
            ).fetchone()[0]
            == 1
        )
        assert {
            row[0]
            for row in old_writer.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'consent_%'"
            ).fetchall()
        } == {
            "consent_control_heads",
            "consent_issuance_receipts",
            "consent_receipts",
            "consent_revocations",
            "consent_deliveries",
        }
        issuance = old_writer.execute("SELECT * FROM consent_issuance_receipts").fetchall()
        assert len(issuance) == 1
        attempted_inserts = []
        old_writer.set_trace_callback(
            lambda statement: (
                attempted_inserts.append(statement)
                if statement.startswith("INSERT INTO approval_grants")
                else None
            )
        )
        old_payload = json.loads(baseline[0][1])
        old_payload["approval_id"] = "old-writer-after-026"
        old_payload["assertion_nonce"] = "old-writer-distinct-nonce"
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(contract_compatibility, "SUPPORTED_RUNTIME_READER", 3)
            with pytest.raises(contract_compatibility.UnsupportedRuntimeContract):
                contract_compatibility.check_runtime_floor(old_writer)
                old_writer.execute(
                    "INSERT INTO approval_grants (approval_id,payload,issued_at,status) "
                    "VALUES (?, ?, ?, 'pending')",
                    ("old-writer-after-026", json.dumps(old_payload), grant.issued_at.isoformat()),
                )
                old_writer.commit()
        assert attempted_inserts == []
        assert old_writer.execute("SELECT * FROM approval_grants").fetchall() == baseline
        assert old_writer.execute("SELECT * FROM runtime_contract_floor").fetchone() == (
            floor[0],
            4,
            *floor[2:],
        )
    await database.initialize()
    assert database.connection.execute("SELECT * FROM approval_grants").fetchall() == baseline
    assert (
        database.connection.execute("SELECT * FROM consent_issuance_receipts").fetchall()
        == issuance
    )
    assert database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == (
        floor[0],
        4,
        *floor[2:],
    )
    repository = ApprovalGrantRepository(database)
    head = repository.get_consent_control_sync(tenant_id="default", household_id="default")
    request = dict(
        tenant_id="default",
        household_id="default",
        approval_id=grant.approval_id,
        expected_version=head,
        principal_id="operator",
        request_id="interrupted-r",
        now=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )
    receipt = repository.revoke_consent_sync(**request)
    await database.close()
    await database.initialize()
    assert repository.revoke_consent_sync(**request) == receipt
    assert database.connection.execute("SELECT * FROM approval_grants").fetchall() == baseline
    assert (
        database.connection.execute("SELECT * FROM consent_issuance_receipts").fetchall()
        == issuance
    )
    assert database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == (
        floor[0],
        4,
        *floor[2:],
    )
    await database.close()


@pytest.mark.asyncio
async def test_t008_consent_upgrade_from_025_preserves_grants_and_blocks_old_reader(tmp_path):
    """Break: upgrade loses consumed evidence or lets a pre-consent reader write."""
    from domoai.persistence import contract_compatibility
    from domoai.persistence.repositories import ApprovalGrantRepository

    db = SQLiteDatabase(tmp_path / "upgrade.sqlite")
    grant = _t008_seed_real_025(db.path)
    await db.initialize()
    names = {
        row[0] for row in db.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "consent_control_heads",
        "consent_revocations",
        "consent_receipts",
        "consent_issuance_receipts",
    } <= names
    repository = ApprovalGrantRepository(db)
    head = repository.get_consent_control_sync(tenant_id="default", household_id="default")
    assert head.revision == 0
    assert repository.get_sync(grant.approval_id) == grant
    row = db.connection.execute("SELECT status, consumed_at FROM approval_grants").fetchone()
    assert tuple(row) == ("consumed", "2026-09-13T12:00:00+00:00")
    request = dict(
        tenant_id="default",
        household_id="default",
        approval_id=grant.approval_id,
        expected_version=head,
        principal_id="operator",
        request_id="upgrade-r",
        now=datetime(2026, 9, 13, 12, tzinfo=UTC),
    )
    receipt = repository.revoke_consent_sync(**request)
    await db.close()
    await db.initialize()
    assert (
        repository.get_consent_control_sync(tenant_id="default", household_id="default")
        == receipt.control_version
    )
    assert repository.revoke_consent_sync(**request) == receipt
    assert repository.get_consent_revocation_sync(grant.approval_id) == receipt
    assert (
        db.connection.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[0]
        == 4
    )
    await db.close()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(contract_compatibility, "SUPPORTED_RUNTIME_READER", 3)
        with pytest.raises(contract_compatibility.UnsupportedRuntimeContract):
            await db.initialize()


@pytest.mark.asyncio
async def test_t008_postgres_upgrade_retry_preserves_consent_and_rejects_reader_three(tmp_path):
    """Break: PG upgrade diverges from 026, reboots lose R, or floor3 writes proceed."""
    import json
    from dataclasses import asdict

    from tests.fixtures.household_onboarding import connection_databases
    from tests.unit.persistence.test_approval_grant_repository import _grant

    from domoai.persistence import contract_compatibility
    from domoai.persistence.repositories import ApprovalGrantRepository

    async with connection_databases(tmp_path, "postgres") as (db, observer):
        # Reconstruct the preceding additive schema in this disposable empty DB.
        observer.connection.rollback()
        for table in (
            "consent_revocations",
            "consent_receipts",
            "consent_issuance_receipts",
            "consent_control_heads",
        ):
            db.connection.execute(f"DROP TABLE {table}")
        db.connection.execute("DELETE FROM schema_migrations WHERE filename='postgres-schema-006'")
        db.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=3")
        grant = _grant()
        payload = asdict(grant)
        payload["authority"] = grant.authority.model_dump(mode="json")
        db.connection.execute(
            "INSERT INTO approval_grants VALUES (?, ?, ?, ?, 'consumed', ?)",
            (
                grant.approval_id,
                json.dumps(payload, default=str),
                grant.issued_at.isoformat(),
                grant.expires_at.isoformat(),
                "2026-09-13T12:00:00+00:00",
            ),
        )
        db.connection.commit()
        await db.close()
        await db.initialize()
        repository = ApprovalGrantRepository(db)
        head = repository.get_consent_control_sync(tenant_id="default", household_id="default")
        assert head.revision == 0
        request = dict(
            tenant_id="default",
            household_id="default",
            approval_id=grant.approval_id,
            expected_version=head,
            principal_id="operator",
            request_id="upgrade-r",
            now=datetime(2026, 9, 13, 12, tzinfo=UTC),
        )
        receipt = repository.revoke_consent_sync(**request)
        await db.close()
        await db.initialize()
        assert repository.get_sync(grant.approval_id) == grant
        assert repository.revoke_consent_sync(**request) == receipt
        assert tuple(
            db.connection.execute("SELECT status, consumed_at FROM approval_grants").fetchone()
        ) == ("consumed", "2026-09-13T12:00:00+00:00")
        assert (
            repository.get_issuance_receipt_sync(assertion_nonce=grant.assertion_nonce).approval_id
            == grant.approval_id
        )
        await db.close()
        before = observer.connection.execute(
            "SELECT * FROM schema_migrations ORDER BY filename"
        ).fetchall()
        observer.connection.rollback()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(contract_compatibility, "SUPPORTED_RUNTIME_READER", 3)
            with pytest.raises(contract_compatibility.UnsupportedRuntimeContract):
                await db.initialize()
        assert (
            observer.connection.execute(
                "SELECT minimum_reader FROM runtime_contract_floor"
            ).fetchone()[0]
            == 4
        )
        assert (
            observer.connection.execute(
                "SELECT * FROM schema_migrations ORDER BY filename"
            ).fetchall()
            == before
        )


@pytest.mark.asyncio
async def test_t008_failed_026_rolls_back_schema_and_retries_without_lost_ledger(tmp_path):
    """Break: partial 026 survives failure or retry skips the complete expansion."""
    import shutil
    import sqlite3

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in MIGRATIONS_DIR.glob("*.sql"):
        if source.name < "026":
            shutil.copyfile(source, migrations / source.name)
    database = SQLiteDatabase(tmp_path / "failed-026.sqlite")
    grant = _t008_seed_real_025(database.path)
    with sqlite3.connect(database.path) as raw:
        ledger = raw.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall()
        baseline = raw.execute("SELECT * FROM approval_grants").fetchall()
        floor = raw.execute("SELECT * FROM runtime_contract_floor").fetchone()
        assert floor[1] == 3
    migration = MIGRATIONS_DIR / "026_household_consent.sql"
    candidate = migrations / migration.name
    candidate.write_text(migration.read_text() + "\nINSERT INTO missing_t008_table VALUES (1);\n")
    with pytest.raises(sqlite3.DatabaseError, match="missing_t008_table"):
        await database.initialize(migrations_dir=migrations)
    with sqlite3.connect(database.path) as raw:
        assert raw.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall() == ledger
        assert raw.execute("SELECT * FROM runtime_contract_floor").fetchone() == floor
        assert raw.execute("SELECT * FROM approval_grants").fetchall() == baseline
        assert (
            raw.execute("SELECT name FROM sqlite_master WHERE name LIKE 'consent_%'").fetchall()
            == []
        )
    shutil.copyfile(migration, candidate)
    await database.initialize(migrations_dir=migrations)
    assert database.connection.execute("SELECT * FROM approval_grants").fetchall() == baseline
    assert database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == (
        floor[0],
        4,
        *floor[2:],
    )
    issuance = database.connection.execute("SELECT * FROM consent_issuance_receipts").fetchall()
    assert len(issuance) == 1 and issuance[0][0] == grant.approval_id
    assert (
        database.connection.execute("SELECT COUNT(*) FROM consent_revocations").fetchone()[0] == 0
    )
    assert (
        database.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        == len(ledger) + 1
    )
    await database.close()
    await database.initialize(migrations_dir=migrations)
    assert (
        database.connection.execute("SELECT * FROM consent_issuance_receipts").fetchall()
        == issuance
    )
    assert database.connection.execute("SELECT * FROM approval_grants").fetchall() == baseline
    assert database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == (
        floor[0],
        4,
        *floor[2:],
    )
    await database.close()


def _migration_filenames() -> list[str]:
    return sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))


@pytest.mark.asyncio
async def test_fresh_database_applies_and_records_every_migration(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()

    rows = database.connection.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()
    assert [row[0] for row in rows] == _migration_filenames()


@pytest.mark.asyncio
async def test_reinitializing_an_already_migrated_database_is_not_reapplied(tmp_path) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()
    first_count = database.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
        0
    ]

    second = SQLiteDatabase(db_path)
    await second.initialize()
    second_count = second.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]

    assert second_count == first_count == len(_migration_filenames())


@pytest.mark.asyncio
async def test_only_not_yet_applied_migrations_run_on_a_partially_migrated_database(
    tmp_path,
) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()

    # Simulate an older deployment that only ever had 001_initial.sql applied:
    # forget every later migration from the ledger without dropping their tables.
    database.connection.execute("DELETE FROM schema_migrations WHERE filename != '001_initial.sql'")
    database.connection.commit()

    reopened = SQLiteDatabase(db_path)
    await reopened.initialize()

    rows = reopened.connection.execute(
        "SELECT filename FROM schema_migrations ORDER BY filename"
    ).fetchall()
    assert [row[0] for row in rows] == _migration_filenames()


@pytest.mark.asyncio
async def test_upgrade_preserves_existing_data_and_enables_new_tables(tmp_path) -> None:
    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize()

    # Simulate a database created under an older deployment: only
    # 001_initial.sql was ever recorded as applied (even though the fixture
    # already created every table, matching a real accidentally-idempotent
    # legacy database per the spec's own Assumptions).
    database.connection.execute("DELETE FROM schema_migrations WHERE filename != '001_initial.sql'")
    database.connection.commit()

    plan_repository = PlanRepository(database)
    original_plan = Plan(
        id="plan-upgrade-1",
        commands=[
            Command(
                id="command-upgrade-1",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key="intent-upgrade-1",
            )
        ],
    )
    await plan_repository.save(original_plan)

    upgraded = SQLiteDatabase(db_path)
    await upgraded.initialize()

    reloaded_plan = await PlanRepository(upgraded).get("plan-upgrade-1")
    assert reloaded_plan is not None
    assert reloaded_plan.id == original_plan.id
    assert reloaded_plan.commands[0].id == "command-upgrade-1"

    scheduled_repository = ScheduledPlanRepository(upgraded)
    scheduled_plan = Plan(
        id="plan-upgrade-scheduled-1",
        execute_at=datetime.now(UTC),
        commands=[
            Command(
                id="command-upgrade-scheduled-1",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key="intent-upgrade-scheduled-1",
            )
        ],
    )
    await scheduled_repository.schedule(scheduled_plan)
    result = await scheduled_repository.get("plan-upgrade-scheduled-1")
    assert result is not None
    assert result[1] == "pending"


@pytest.mark.asyncio
async def test_non_idempotent_migration_survives_a_second_initialization(tmp_path) -> None:
    migrations_dir = tmp_path / "custom_migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_create.sql").write_text(
        "CREATE TABLE widgets (id TEXT PRIMARY KEY);", encoding="utf-8"
    )
    (migrations_dir / "002_add_column.sql").write_text(
        "ALTER TABLE widgets ADD COLUMN label TEXT;", encoding="utf-8"
    )

    db_path = tmp_path / "repo.sqlite3"
    database = SQLiteDatabase(db_path)
    await database.initialize(migrations_dir=migrations_dir)

    columns_first = {row[1] for row in database.connection.execute("PRAGMA table_info(widgets)")}
    assert "label" in columns_first

    reopened = SQLiteDatabase(db_path)
    await reopened.initialize(migrations_dir=migrations_dir)

    columns_second = {row[1] for row in reopened.connection.execute("PRAGMA table_info(widgets)")}
    assert columns_second == columns_first
