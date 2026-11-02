import hashlib
import json

import pytest

from domoai.domain.errors import DomainError
from domoai.persistence.repositories import PlanRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from tests.fixtures.runtime_schema_compatibility import NOW, plan


def historical_payload(*, status="ready", record_id="historical") -> str:
    payload = plan(id=record_id, deployment_id="deployment").model_dump(mode="json")
    payload.update(schema_version="v1", status=status, definition_digest="sha256:original")
    return json.dumps(payload, indent=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["draft", "ready", "approved", "executing", "completed", "unknown"]
)
async def test_incompatible_plan_active_read_is_sanitized_and_preserves_raw(
    tmp_path, status
) -> None:
    database = SQLiteDatabase(tmp_path / "history.sqlite")
    await database.initialize()
    try:
        raw = historical_payload(status=status)
        database.connection.execute(
            "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
            ("historical", raw, status, NOW.isoformat()),
        )
        database.connection.commit()
        with pytest.raises(DomainError) as error:
            await PlanRepository(database, deployment_id="deployment").get("historical")
        assert error.value.details["reason"] in {"historical_only", "revalidation_required"}
        assert "commands" not in json.dumps(error.value.details)
        assert database.connection.execute("SELECT payload FROM plans").fetchone()[0] == raw
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_raw_history_is_scoped_nonexecutable_and_byte_preserving(tmp_path) -> None:
    from domoai.persistence.contract_compatibility import inspect_record

    database = SQLiteDatabase(tmp_path / "history.sqlite")
    await database.initialize()
    try:
        raw = historical_payload(status="unknown")
        database.connection.execute(
            "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
            ("historical", raw, "unknown", NOW.isoformat()),
        )
        database.connection.commit()
        record = inspect_record(
            database.connection,
            record_kind="plan",
            record_id="historical",
            tenant_id="default",
            household_id="default",
            deployment_id="deployment",
        )
        assert record.raw_payload == raw
        assert record.payload_sha256 == hashlib.sha256(raw.encode()).hexdigest()
        assert record.as_response()["executable"] is False
        assert record.as_response()["compatibility_reason"] == "historical_only"
        assert record.as_response()["payload"] == json.loads(raw)
        with pytest.raises(DomainError):
            inspect_record(
                database.connection,
                record_kind="plan",
                record_id="historical",
                tenant_id="foreign",
                household_id="default",
                deployment_id="deployment",
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_incompatible_schedule_does_not_abort_compatible_due_scan(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "schedule.sqlite")
    await database.initialize()
    try:
        good = plan(id="good", execute_at=NOW)
        await ScheduledPlanRepository(database).schedule(good)
        raw = historical_payload()
        database.connection.execute(
            "INSERT INTO scheduled_plans VALUES(?,?,?,?,?)",
            ("historical", NOW.isoformat(), "pending", raw, NOW.isoformat()),
        )
        database.connection.commit()
        repository = ScheduledPlanRepository(database)
        assert [item.id for item in await repository.list_pending()] == ["good"]
        blocks = await repository.compatibility_diagnostics(
            tenant_id="default", household_id="default", deployment_id="deployment"
        )
        assert blocks["records"] == [
            {
                "record_kind": "scheduled_plan",
                "record_id": "historical",
                "reason": "revalidation_required",
            }
        ]
        assert (
            database.connection.execute(
                "SELECT payload FROM scheduled_plans WHERE plan_id='historical'"
            ).fetchone()[0]
            == raw
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["initialize", "open_existing"])
@pytest.mark.parametrize("floor", [999, -1, "malformed"])
async def test_unsupported_floor_rejected_before_any_sqlite_bootstrap_write(
    tmp_path, method, floor
) -> None:
    import sqlite3

    path = tmp_path / "future.sqlite"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE runtime_contract_floor(singleton INTEGER, minimum_reader, "
        "checkpoint_id TEXT, created_at TEXT)"
    )
    connection.execute(
        "INSERT INTO runtime_contract_floor VALUES(1,?,?,?)",
        (floor, "future-checkpoint", NOW.isoformat()),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()
    database = SQLiteDatabase(path)
    try:
        with pytest.raises(RuntimeError, match="runtime contract floor"):
            await getattr(database, method)()
        assert database._connection is None
        assert path.read_bytes() == before
        assert not path.with_name(path.name + "-wal").exists()
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_supported_floor_is_seeded_once_and_survives_restart(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "floor.sqlite")
    await database.initialize()
    try:
        row = database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
        assert row[1] == 4
        database.connection.execute(
            "UPDATE runtime_contract_floor SET checkpoint_id='approved-checkpoint'"
        )
        database.connection.commit()
    finally:
        await database.close()
    await database.initialize()
    try:
        assert (
            database.connection.execute(
                "SELECT checkpoint_id FROM runtime_contract_floor"
            ).fetchone()[0]
            == "approved-checkpoint"
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_offline_admin_history_uses_configured_scope_and_reports_activation_block(
    tmp_path, monkeypatch
) -> None:
    from domoai.admin.cli import _parser, _runtime_contract
    from domoai.config.settings import Settings

    path = tmp_path / "admin.sqlite"
    database = SQLiteDatabase(path)
    await database.initialize()
    raw = historical_payload(status="unknown")
    database.connection.execute(
        "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
        ("historical", raw, "unknown", NOW.isoformat()),
    )
    database.connection.commit()
    await database.close()
    monkeypatch.setattr(
        Settings,
        "from_environment",
        lambda: Settings(
            database_path=path,
            mcp_deployment_id="deployment",
            connection_master_key_path=tmp_path / "key",
        ),
    )
    args = _parser().parse_args(
        ["runtime-contract", "history", "--record-kind", "plan", "--record-id", "historical"]
    )
    response = await _runtime_contract(args)
    assert response["payload"] == json.loads(raw)
    assert response["executable"] is False
    args = _parser().parse_args(["runtime-contract", "preflight"])
    preflight = await _runtime_contract(args)
    assert preflight["activation"] == "blocked_pending_operational_exclusion"
    assert preflight["records"][0]["record_id"] == "historical"


@pytest.mark.asyncio
async def test_nonobject_history_cannot_abort_scan_or_disclose_unscoped_id(tmp_path) -> None:
    from domoai.persistence.contract_compatibility import compatibility_diagnostics

    database = SQLiteDatabase(tmp_path / "corrupt.sqlite")
    await database.initialize()
    try:
        database.connection.execute(
            "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
            ("secret-unscoped-id", "[]", "ready", NOW.isoformat()),
        )
        database.connection.commit()
        with pytest.raises(DomainError):
            await PlanRepository(database).get("secret-unscoped-id")
        report = compatibility_diagnostics(
            database.connection,
            record_kind="plan",
            tenant_id="default",
            household_id="default",
            deployment_id="deployment",
            limit=1,
        )
        assert report["records"] == []
        assert report["next_offset"] is None
        assert "secret-unscoped-id" not in json.dumps(report)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_physical_replay_rejects_payload_projection_mismatch(tmp_path) -> None:
    from domoai.domain.coordination import PhysicalIntent
    from domoai.persistence.coordination import PhysicalIntentRepository

    database = SQLiteDatabase(tmp_path / "physical.sqlite")
    await database.initialize()
    try:
        intent = PhysicalIntent(
            tenant_id="default",
            household_id="default",
            deployment_id="deployment",
            idempotency_key="intent",
            plan_id="plan",
            command_id="command",
            fencing_epoch=1,
            owner_id="owner",
            execution_attempt_id="attempt",
            lease_id="lease",
            created_at=NOW,
            updated_at=NOW,
        )
        repository = PhysicalIntentRepository(database)
        await repository.claim_for_dispatch(intent)
        database.connection.execute("UPDATE physical_intents SET status='unknown'")
        database.connection.commit()
        before = database.connection.execute(
            "SELECT payload,status FROM physical_intents"
        ).fetchone()
        with pytest.raises(ValueError, match="projection"):
            await repository.claim_for_dispatch(intent)
        with pytest.raises(ValueError, match="projection"):
            await repository.get(household_id="default", idempotency_key="intent")
        assert (
            database.connection.execute("SELECT payload,status FROM physical_intents").fetchone()
            == before
        )
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_diagnostic_pages_depend_only_on_authorized_household(tmp_path, backend) -> None:
    from contextlib import ExitStack

    from domoai.persistence.postgres import PostgresDatabase

    with ExitStack() as stack:
        if backend == "postgres":
            from testcontainers.community.postgres import PostgresContainer

            container = stack.enter_context(
                PostgresContainer(
                    "postgres:16-alpine",
                    username="domoai",
                    password="domoai-test",
                    dbname="domoai",
                    driver=None,
                )
            )
            database = PostgresDatabase(container.get_connection_url(driver=None))
        else:
            database = SQLiteDatabase(tmp_path / "pages.sqlite")
        await database.initialize()
        try:
            for index in range(20):
                payload = json.loads(historical_payload(record_id=f"row-{index:02}"))
                if index not in {4, 18}:
                    payload["authority"]["tenant_id"] = "foreign"
                database.connection.execute(
                    "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
                    (payload["id"], json.dumps(payload), "ready", NOW.isoformat()),
                )
            database.connection.commit()
            repository = PlanRepository(database)
            scope = dict(
                tenant_id="default", household_id="default", deployment_id="deployment", limit=1
            )
            first = await repository.compatibility_diagnostics(**scope)
            second = await repository.compatibility_diagnostics(
                **scope, offset=first["next_offset"]
            )
            last = await repository.compatibility_diagnostics(**scope, offset=second["next_offset"])
            assert first == {
                "records": [
                    {
                        "record_kind": "plan",
                        "record_id": "row-04",
                        "reason": "revalidation_required",
                    }
                ],
                "next_offset": 1,
            }
            assert second["records"][0]["record_id"] == "row-18"
            assert second["next_offset"] == 2
            assert last == {"records": [], "next_offset": None}
        finally:
            await database.close()


@pytest.mark.asyncio
async def test_bundle_recovery_holds_incompatible_member_without_aborting_other_bundles(
    tmp_path,
) -> None:
    from domoai.application.bundle_commit import BundleRecoveryService
    from domoai.domain.models import BundleCommit, BundleMemberCommit
    from domoai.persistence.repositories import BundleCommitRepository
    from domoai.runtime.events import AuditLog

    database = SQLiteDatabase(tmp_path / "bundle.sqlite")
    await database.initialize()
    try:
        raw = historical_payload(status="executing")
        database.connection.execute(
            "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
            ("historical", raw, "executing", NOW.isoformat()),
        )
        database.connection.commit()
        bundles = BundleCommitRepository(database)
        bundle = BundleCommit(
            id="historic-bundle",
            bundle_digest="original-bundle",
            scenario_id="scenario",
            members=[BundleMemberCommit(plan_id="historical", validation_digest="original")],
        )
        await bundles.save(bundle)
        before = database.connection.execute("SELECT payload FROM bundle_commits").fetchone()[0]
        await bundles.save(
            BundleCommit(
                id="compatible-bundle",
                bundle_digest="compatible-digest",
                scenario_id="scenario",
                members=[BundleMemberCommit(plan_id="absent-plan", validation_digest="valid")],
            )
        )
        audit = AuditLog()
        recovery = BundleRecoveryService(
            bundle_repository=bundles,
            scheduled_repository=ScheduledPlanRepository(database),
            audit=audit,
            plan_repository=PlanRepository(database),
        )
        assert await recovery.recover_orphaned_bundles() == ["compatible-bundle"]
        assert (
            database.connection.execute(
                "SELECT payload FROM bundle_commits WHERE id='historic-bundle'"
            ).fetchone()[0]
            == before
        )
        assert database.connection.execute("SELECT payload FROM plans").fetchone()[0] == raw
    finally:
        await database.close()


def test_malformed_raw_contract_is_sanitized_without_echoing_bytes() -> None:
    from domoai.persistence.contract_compatibility import read_active_plan, read_active_rule

    for reader in (read_active_plan, read_active_rule):
        with pytest.raises(DomainError) as error:
            reader('{"secret":"do-not-echo"')
        assert "do-not-echo" not in str(error.value)
        assert "do-not-echo" not in json.dumps(error.value.details)


@pytest.mark.asyncio
async def test_writer_elevates_supported_floor_without_relabelling_checkpoint(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "elevate.sqlite")
    await database.initialize()
    database.connection.execute(
        "UPDATE runtime_contract_floor SET minimum_reader=1, checkpoint_id='original-checkpoint'"
    )
    database.connection.commit()
    original = database.connection.execute(
        "SELECT checkpoint_id,created_at FROM runtime_contract_floor"
    ).fetchone()
    await database.close()
    await database.open_existing()
    assert (
        database.connection.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[
            0
        ]
        == 1
    )
    await database.close()
    await database.initialize()
    try:
        assert (
            database.connection.execute(
                "SELECT minimum_reader FROM runtime_contract_floor"
            ).fetchone()[0]
            == 4
        )
        assert (
            database.connection.execute(
                "SELECT checkpoint_id,created_at FROM runtime_contract_floor"
            ).fetchone()
            == original
        )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_floor_elevation_failure_rolls_back_and_closes_connection(tmp_path) -> None:
    import sqlite3

    path = tmp_path / "failed-elevation.sqlite"
    database = SQLiteDatabase(path)
    await database.initialize()
    database.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=1")
    database.connection.execute(
        "CREATE TRIGGER reject_floor BEFORE UPDATE ON runtime_contract_floor "
        "BEGIN SELECT RAISE(ABORT, 'injected floor failure'); END"
    )
    database.connection.commit()
    original = database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
    await database.close()
    with pytest.raises(sqlite3.IntegrityError, match="injected floor failure"):
        await database.initialize()
    assert database._connection is None
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT * FROM runtime_contract_floor").fetchone() == original


@pytest.mark.asyncio
async def test_concurrent_sqlite_floor_elevation_preserves_checkpoint(
    tmp_path, monkeypatch
) -> None:
    import asyncio
    import sqlite3
    import threading

    path = tmp_path / "concurrent-elevation.sqlite"
    seed = SQLiteDatabase(path)
    await seed.initialize()
    seed.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=1")
    seed.connection.commit()
    original = seed.connection.execute(
        "SELECT checkpoint_id,created_at FROM runtime_contract_floor"
    ).fetchone()
    await seed.close()

    competing_snapshots = threading.Barrier(2)
    connections = []

    class ConcurrentConnection(sqlite3.Connection):
        reserved = False

        def execute(self, sql, *args, **kwargs):
            if sql.startswith("UPDATE runtime_contract_floor SET minimum_reader = 4"):
                if not self.reserved:
                    # Both unreserved readers have observed floor1. Never rendezvous
                    # while a writer owns the exclusive reservation being tested.
                    competing_snapshots.wait(timeout=5)
            result = super().execute(sql, *args, **kwargs)
            if sql == "BEGIN IMMEDIATE":
                self.reserved = True
                self.order.append("reserved")
            if sql.startswith("SELECT singleton, minimum_reader") and self.autocommit is False:
                self.order.append("floor_read")
            return result

    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=ConcurrentConnection)
        connection.order = []
        connections.append(connection)
        return connection

    monkeypatch.setattr("domoai.persistence.sqlite.sqlite3.connect", connect)

    def initialize_writer():
        async def run():
            writer = SQLiteDatabase(path)
            try:
                await writer.initialize()
                return writer.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
            finally:
                await writer.close()

        return asyncio.run(run())

    results = await asyncio.gather(
        *[asyncio.to_thread(initialize_writer) for _ in range(2)], return_exceptions=True
    )
    assert results == [(1, 4, *original)] * 2
    assert len(connections) == 2
    assert all(connection.order == ["reserved", "floor_read"] for connection in connections)


@pytest.mark.asyncio
async def test_physical_recovery_preserves_incompatible_rows_and_progresses(tmp_path) -> None:
    from domoai.domain.coordination import PhysicalIntent
    from domoai.persistence.coordination import PhysicalIntentRepository

    database = SQLiteDatabase(tmp_path / "recovery.sqlite")
    await database.initialize()
    try:
        repository = PhysicalIntentRepository(database)
        for key in ("incompatible", "compatible"):
            await repository.claim_for_dispatch(
                PhysicalIntent(
                    tenant_id="default",
                    household_id="default",
                    deployment_id="deployment",
                    idempotency_key=key,
                    plan_id=key,
                    command_id=key,
                    fencing_epoch=1,
                    owner_id="owner",
                    execution_attempt_id="attempt",
                    lease_id="lease",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        database.connection.execute(
            "UPDATE physical_intents SET owner_id='different-owner' "
            "WHERE idempotency_key='incompatible'"
        )
        database.connection.commit()
        original = database.connection.execute(
            "SELECT * FROM physical_intents WHERE idempotency_key='incompatible'"
        ).fetchone()
        assert await repository.recover_inflight(deployment_id="deployment", fencing_epoch=2) == 1
        assert (
            database.connection.execute(
                "SELECT * FROM physical_intents WHERE idempotency_key='incompatible'"
            ).fetchone()
            == original
        )
        assert (
            await repository.get(household_id="default", idempotency_key="compatible")
        ).status.value == "unknown"
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_scoped_physical_recovery_skips_corrupt_json_on_real_stores(
    tmp_path, backend
) -> None:
    from contextlib import ExitStack

    from testcontainers.community.postgres import PostgresContainer

    from domoai.domain.coordination import PhysicalIntent
    from domoai.persistence.coordination import PhysicalIntentRepository
    from domoai.persistence.postgres import PostgresDatabase

    with ExitStack() as stack:
        if backend == "postgres":
            container = stack.enter_context(
                PostgresContainer(
                    "postgres:16-alpine",
                    username="domoai",
                    password="domoai-test",
                    dbname="domoai",
                    driver=None,
                )
            )
            database = PostgresDatabase(container.get_connection_url(driver=None))
        else:
            database = SQLiteDatabase(tmp_path / "corrupt-ledger.sqlite")
        await database.initialize()
        try:
            repository = PhysicalIntentRepository(database)
            for key in ("corrupt", "compatible"):
                await repository.claim_for_dispatch(
                    PhysicalIntent(
                        tenant_id="default",
                        household_id="default",
                        deployment_id="deployment",
                        idempotency_key=key,
                        plan_id=key,
                        command_id=key,
                        fencing_epoch=1,
                        owner_id="owner",
                        execution_attempt_id="attempt",
                        lease_id="lease",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
            database.connection.execute(
                "UPDATE physical_intents SET payload='{broken' WHERE idempotency_key='corrupt'"
            )
            database.connection.commit()
            original = database.connection.execute(
                "SELECT * FROM physical_intents WHERE idempotency_key='corrupt'"
            ).fetchone()
            assert (
                await repository.recover_inflight(deployment_id="deployment", fencing_epoch=2) == 1
            )
            assert (
                database.connection.execute(
                    "SELECT * FROM physical_intents WHERE idempotency_key='corrupt'"
                ).fetchone()
                == original
            )
        finally:
            await database.close()


@pytest.mark.asyncio
async def test_history_without_verifiable_scope_does_not_inherit_default_household(
    tmp_path,
) -> None:
    from domoai.persistence.contract_compatibility import inspect_record

    database = SQLiteDatabase(tmp_path / "unscoped-history.sqlite")
    await database.initialize()
    try:
        raw = json.dumps({"id": "unscoped", "status": "unknown", "private": "foreign-history"})
        database.connection.execute(
            "INSERT INTO plans(id,payload,status,updated_at) VALUES(?,?,?,?)",
            ("unscoped", raw, "unknown", NOW.isoformat()),
        )
        database.connection.commit()
        with pytest.raises(DomainError):
            inspect_record(
                database.connection,
                record_kind="plan",
                record_id="unscoped",
                tenant_id="default",
                household_id="default",
                deployment_id="deployment",
            )
        assert database.connection.execute("SELECT payload FROM plans").fetchone()[0] == raw
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_floor_elevation_closes_after_commit_and_rollback_both_fail(tmp_path, monkeypatch):
    import sqlite3

    database = SQLiteDatabase(tmp_path / "double-failure.sqlite")
    await database.initialize()
    database.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=1")
    database.connection.commit()
    original = database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
    await database.close()
    opened = []

    class FaultConnection(sqlite3.Connection):
        updated = False
        commit_failed = False
        rollback_failed = False
        closed = False

        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.startswith("UPDATE runtime_contract_floor SET minimum_reader = 4"):
                self.updated = True
            return result

        def commit(self):
            if self.updated and not self.commit_failed:
                self.commit_failed = True
                raise sqlite3.OperationalError("injected commit failure")
            return super().commit()

        def rollback(self):
            if self.updated and not self.rollback_failed:
                self.rollback_failed = True
                raise sqlite3.OperationalError("injected rollback failure")
            return super().rollback()

        def close(self):
            super().close()
            self.closed = True

    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=FaultConnection)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr("domoai.persistence.sqlite.sqlite3.connect", connect)
        with pytest.raises(sqlite3.OperationalError):
            await database.initialize()
    assert opened[0].commit_failed and opened[0].rollback_failed
    assert database._connection is None
    assert opened[0].closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    await database.open_existing()
    assert (
        database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == original
    )
    database.connection.execute("BEGIN IMMEDIATE")
    database.connection.rollback()
    await database.close()
    await database.initialize()
    assert database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == (
        original[0],
        4,
        *original[2:],
    )
    await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_configured_recovery_preserves_legacy_definition_and_grant_on_real_stores(
    tmp_path, backend
):
    from contextlib import ExitStack
    from datetime import timedelta

    from testcontainers.community.postgres import PostgresContainer

    from domoai.application.executor import PlanExecutor
    from domoai.application.plan_service import PlanService
    from domoai.application.recovery import PlanRecoveryService
    from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
    from domoai.domain.models import Command, PlanExecutionLease, PlanStatus
    from domoai.persistence.coordination import PhysicalIntentRepository
    from domoai.persistence.postgres import PostgresDatabase
    from domoai.persistence.repositories import ApprovalGrantRepository
    from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
    from domoai.runtime.approval_store import ApprovalAssertion, ApprovalStore, OperatorPrincipal
    from domoai.runtime.clock import FixedClock
    from domoai.runtime.events import AuditLog
    from tests.unit.application.test_plan_service import build_service_with_adapter

    with ExitStack() as stack:
        if backend == "postgres":
            container = stack.enter_context(
                PostgresContainer(
                    "postgres:16-alpine",
                    username="domoai",
                    password="domoai-test",
                    dbname="domoai",
                    driver=None,
                )
            )
            database = PostgresDatabase(container.get_connection_url(driver=None))
        else:
            database = SQLiteDatabase(tmp_path / "legacy-recovery.sqlite")
        await database.initialize()
        storage = SerializedStorageExecutor()
        try:
            adapter, legacy_service = await build_service_with_adapter()
            now = legacy_service.clock.now()
            cover = next(d for d in legacy_service.registry.devices if d.type.value == "cover")
            legacy = legacy_service.validate(
                legacy_service.create_plan(
                    "legacy-approved",
                    [
                        Command(
                            id="legacy-command",
                            device_id=cover.id,
                            command="open",
                            risk_class="confirm",
                            idempotency_key="legacy-intent",
                        )
                    ],
                )
            )
            grants = ApprovalStore(persistence=ApprovalGrantRepository(database))
            principal = OperatorPrincipal("operator", "oidc:mfa", "session")
            grant = grants.issue_authenticated(
                legacy,
                principal=principal,
                assertion=ApprovalAssertion(
                    principal=principal,
                    plan_id=legacy.id,
                    validation_digest=legacy.validation.digest,
                    nonce="original-consent",
                    approved_at=now,
                    expires_at=now + timedelta(minutes=2),
                ),
            )
            grant = grants.consume(grant.approval_id, legacy)
            legacy = legacy_service.approve(legacy, grant=grant).model_copy(
                update={"status": PlanStatus.EXECUTING}
            )
            originals = {legacy.id: legacy}
            light = next(d for d in legacy_service.registry.devices if d.type.value == "light")
            for identity, deployment, with_lease in (
                ("current", "edge", True),
                ("missing-lease", "edge", False),
                ("foreign", "other", True),
            ):
                service = PlanService(
                    legacy_service.registry,
                    legacy_service.state_store,
                    legacy_service.policy_engine,
                    legacy_service.audit,
                    deployment_id=deployment,
                )
                current = service.validate(
                    service.create_plan(
                        identity,
                        [
                            Command(
                                id=identity,
                                device_id=light.id,
                                command="turn_on",
                                idempotency_key=identity,
                            )
                        ],
                    )
                )
                lease = (
                    PlanExecutionLease(
                        deployment_id=deployment,
                        owner_id="old-owner",
                        lease_id=identity,
                        fencing_epoch=1,
                        expires_at=now - timedelta(seconds=1),
                    )
                    if with_lease
                    else None
                )
                originals[identity] = current.model_copy(
                    update={"status": PlanStatus.EXECUTING, "execution_lease": lease}
                )
            unscoped_repository = PlanRepository(database)
            for original in originals.values():
                await unscoped_repository.save(original)
            legacy_payload = legacy.model_dump(mode="json")
            legacy_payload.pop("execution")
            assert type(legacy).model_validate(legacy_payload) == legacy
            database.connection.execute(
                "UPDATE plans SET payload=? WHERE id=?",
                (json.dumps(legacy_payload, sort_keys=True), legacy.id),
            )
            database.connection.commit()
            ledger = PhysicalIntentRepository(database)
            claimed = await ledger.claim_for_dispatch(
                PhysicalIntent(
                    tenant_id="default",
                    household_id="default",
                    deployment_id="original-edge",
                    idempotency_key="legacy-intent",
                    plan_id=legacy.id,
                    command_id="legacy-command",
                    fencing_epoch=1,
                    owner_id="original-owner",
                    execution_attempt_id="original-attempt",
                    lease_id="original-lease",
                    created_at=now,
                    updated_at=now,
                )
            )
            await ledger.settle(intent=claimed.intent, status=PhysicalIntentStatus.UNKNOWN)
            ledger_bytes = database.connection.execute("SELECT * FROM physical_intents").fetchall()
            grant_bytes = database.connection.execute("SELECT * FROM approval_grants").fetchall()
            before_rows = {
                row[0]: row[1:]
                for row in database.connection.execute(
                    "SELECT id,payload,status,updated_at FROM plans"
                ).fetchall()
            }
            clock = FixedClock(now + timedelta(seconds=1))
            repository = SerializedRepositoryProxy(
                PlanRepository(database, deployment_id="edge", clock=clock), storage
            )
            audit = AuditLog()
            fenced = PlanRecoveryService(
                repository, audit, deployment_id="edge", fencing_epoch=2, clock=clock
            )
            assert await fenced.reconcile() == ["current"]
            assert (await repository.get(legacy.id)).status is PlanStatus.EXECUTING
            assert (await repository.get("missing-lease")).status is PlanStatus.EXECUTING
            assert (await repository.get("foreign")).model_dump() == originals[
                "foreign"
            ].model_dump()
            local = PlanRecoveryService(repository, audit, deployment_id="edge", clock=clock)
            assert set(await local.recover_orphaned_plans()) == {legacy.id, "missing-lease"}
            rows = await storage.run(
                lambda: database.connection.execute(
                    "SELECT id,payload,status,updated_at FROM plans"
                ).fetchall()
            )
            for identity, payload, status, updated_at in rows:
                if identity == "foreign":
                    assert (payload, status, updated_at) == before_rows[identity]
                    continue
                assert json.loads(payload) == json.loads(before_rows[identity][0]) | {
                    "status": "unknown"
                }
                assert status == "unknown" and updated_at == clock.now().isoformat()
                assert (await repository.get(identity)).status is PlanStatus.UNKNOWN
            recovered = await repository.get(legacy.id)
            assert recovered.schema_version == "v1" and recovered.deployment_id is None
            assert recovered.execution_lease is None
            assert (
                recovered.approval == legacy.approval and recovered.validation == legacy.validation
            )
            assert recovered.definition_digest == legacy.definition_digest
            assert (
                await storage.run(
                    lambda: database.connection.execute("SELECT * FROM approval_grants").fetchall()
                )
                == grant_bytes
            )
            event = next(
                e
                for e in audit.events
                if e.event_type == "plan_execution_recovered" and e.subject_id == legacy.id
            )
            assert event.payload["deployment_id"] == "edge"
            executor = PlanExecutor(adapter, legacy_service, audit, plan_repository=repository)
            with pytest.raises(DomainError):
                await executor.execute(recovered)
            assert not adapter.calls
            assert (
                await storage.run(
                    lambda: database.connection.execute("SELECT * FROM physical_intents").fetchall()
                )
                == ledger_bytes
            )
            await storage.close()
            await database.close()
            await database.open_existing()
            storage = SerializedStorageExecutor()
            restarted = SerializedRepositoryProxy(
                PlanRepository(database, deployment_id="edge"), storage
            )
            assert (await restarted.get(legacy.id)).model_dump() == recovered.model_dump()
            assert (
                await PlanRecoveryService(restarted, AuditLog(), deployment_id="edge").reconcile()
                == []
            )
            assert (
                database.connection.execute("SELECT * FROM approval_grants").fetchall()
                == grant_bytes
            )
            assert (
                database.connection.execute("SELECT * FROM physical_intents").fetchall()
                == ledger_bytes
            )
        finally:
            await storage.close()
            await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["COMMIT", "BEGIN IMMEDIATE", "floor_read"])
async def test_floor_reservation_failure_closes_without_success_commit(
    tmp_path, monkeypatch, phase
):
    import sqlite3

    from domoai.persistence.contract_compatibility import UnsupportedRuntimeContract

    database = SQLiteDatabase(tmp_path / "reservation-failure.sqlite")
    await database.initialize()
    database.connection.execute("UPDATE runtime_contract_floor SET minimum_reader=1")
    database.connection.commit()
    original = database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
    await database.close()
    opened = []

    class FaultConnection(sqlite3.Connection):
        reserved = False
        failed = False
        closed = False
        commits_after_failure = 0

        def execute(self, sql, *args, **kwargs):
            if sql == phase or (
                phase == "floor_read"
                and self.reserved
                and sql.startswith("SELECT singleton, minimum_reader")
            ):
                self.failed = True
                raise sqlite3.OperationalError("injected reservation failure")
            result = super().execute(sql, *args, **kwargs)
            if sql == "BEGIN IMMEDIATE":
                self.reserved = True
            return result

        def commit(self):
            if self.failed:
                self.commits_after_failure += 1
            return super().commit()

        def close(self):
            super().close()
            self.closed = True

    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=FaultConnection)
        opened.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr("domoai.persistence.sqlite.sqlite3.connect", connect)
        expected = UnsupportedRuntimeContract if phase == "floor_read" else sqlite3.OperationalError
        with pytest.raises(expected):
            await database.initialize()
    assert database._connection is None
    assert len(opened) == 1 and opened[0].failed and opened[0].closed
    assert opened[0].commits_after_failure == 0
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")
    await database.open_existing()
    assert (
        database.connection.execute("SELECT * FROM runtime_contract_floor").fetchone() == original
    )
    database.connection.execute("BEGIN IMMEDIATE")
    database.connection.rollback()
    await database.close()
