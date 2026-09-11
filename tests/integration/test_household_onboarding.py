"""Acquisition and cache projection must agree on the current physical source."""

from datetime import timedelta
from uuid import UUID

import pytest

from domoai.domain.models import StateStatus
from tests.fixtures.household_onboarding import (
    connect_home_assistant,
    connection_databases,
    home_assistant_http_source,
    onboarding_gateway,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_secret_transfer_bootstrap_elevates_reader_floor_and_preserves_checkpoint(
    tmp_path, backend
):
    async with connection_databases(tmp_path, backend) as (database, observer):
        row = observer.connection.execute(
            "SELECT minimum_reader, checkpoint_id, created_at FROM runtime_contract_floor"
        ).fetchone()
        observer.connection.commit()
        assert row[0] == 3
        assert (
            observer.connection.execute(
                "SELECT COUNT(*) FROM connection_secret_transfers"
            ).fetchone()[0]
            == 0
        )
        observer.connection.commit()
        database.connection.execute("UPDATE runtime_contract_floor SET minimum_reader = 2")
        database.connection.commit()
        await database.close()
        await database.initialize()
        after = observer.connection.execute(
            "SELECT minimum_reader, checkpoint_id, created_at FROM runtime_contract_floor"
        ).fetchone()
        assert tuple(after) == (3, row[1], row[2])
        observer.connection.commit()


@pytest.mark.asyncio
async def test_floor_migration_failure_rolls_back_new_table_and_ledger(tmp_path):
    import shutil
    import sqlite3

    from domoai.persistence.sqlite import MIGRATIONS_DIR, SQLiteDatabase

    legacy = tmp_path / "legacy-migrations"
    legacy.mkdir()
    for migration in MIGRATIONS_DIR.glob("*.sql"):
        if migration.name < "024":
            shutil.copy2(migration, legacy / migration.name)
    database = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    await database.initialize(migrations_dir=legacy)
    database.connection.execute("UPDATE runtime_contract_floor SET minimum_reader = 2")
    database.connection.commit()
    await database.close()
    observer = sqlite3.connect(database.path)
    try:
        before = observer.execute("SELECT * FROM runtime_contract_floor").fetchall()
        ledger = observer.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall()
        observer.execute("""CREATE TRIGGER reject_floor BEFORE UPDATE ON runtime_contract_floor
            BEGIN SELECT RAISE(ABORT, 'fixture floor failure'); END""")
        observer.commit()
        with pytest.raises(sqlite3.IntegrityError, match="fixture floor failure"):
            await database.initialize()
        assert observer.execute("SELECT * FROM runtime_contract_floor").fetchall() == before
        assert (
            observer.execute("SELECT * FROM schema_migrations ORDER BY filename").fetchall()
            == ledger
        )
        assert (
            observer.execute(
                "SELECT name FROM sqlite_master WHERE name = 'connection_secret_transfers'"
            ).fetchone()
            is None
        )
        observer.execute("DROP TRIGGER reject_floor")
        observer.commit()
        await database.initialize()
        assert (
            observer.execute("SELECT minimum_reader FROM runtime_contract_floor").fetchone()[0] == 3
        )
    finally:
        observer.close()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_exact_reader253_modules_reject_floor3_in_separate_process_without_writes(
    tmp_path, backend
):
    import json
    import subprocess
    import sys
    from pathlib import Path

    golden = Path(__file__).parents[1] / "fixtures/contracts/household-onboarding/reader253"
    script = """
import asyncio, hashlib, importlib.machinery, importlib.util, json, pathlib, sys
golden = pathlib.Path(sys.argv[1])
provenance = json.loads((golden / "provenance.json").read_text())["files"]
loaded = {}
for name in ("contract_compatibility", "sqlite", "postgres"):
    path = golden / (name + ".py.txt")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == provenance[path.name]["sha256"]
    full = "domoai.persistence." + name
    loader = importlib.machinery.SourceFileLoader(full, str(path))
    spec = importlib.util.spec_from_loader(full, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    loader.exec_module(module)
    assert pathlib.Path(module.__file__).resolve() == path.resolve()
    loaded[name] = module
compat = loaded["contract_compatibility"]
assert compat.SUPPORTED_RUNTIME_READER == 2
# Golden schema/migrations are deliberately absent: the floor must reject
# before either backend can load bootstrap SQL. This is not an old binary rollout.
assert not (golden / "postgres_schema.sql").exists()
assert not (golden / "migrations").exists()
database = (loaded["postgres"].PostgresDatabase(sys.argv[3]) if sys.argv[2] == "postgres"
            else loaded["sqlite"].SQLiteDatabase(pathlib.Path(sys.argv[3])))
try:
    asyncio.run(database.initialize())
except compat.UnsupportedRuntimeContract:
    print(json.dumps({"reader": 2, "rejected": True, "modules": list(loaded)}))
else:
    raise AssertionError("reader253 admitted floor3")
"""
    async with connection_databases(tmp_path, backend) as (database, observer):
        tables = (
            "runtime_contract_floor",
            "schema_migrations",
            "connections",
            "connection_ownership",
            "connection_secret_transfers",
        )

        def snapshot():
            result = [
                tuple(
                    map(
                        tuple,
                        observer.connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall(),
                    )
                )
                for table in tables
            ]
            observer.connection.commit()
            return result

        before = snapshot()
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(golden),
                backend,
                database.dsn if backend == "postgres" else str(database.path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["rejected"] is True
        assert snapshot() == before


@pytest.mark.asyncio
async def test_http_ha_acquisition_preserves_source_timestamp_and_never_commands(tmp_path):
    async with home_assistant_http_source() as source, onboarding_gateway(tmp_path) as gateway:
        profile = await connect_home_assistant(gateway, source)
        assert source.reads > 0
        async with gateway.http_client() as client:
            inventory = (await client.get("/api/v1/onboarding/inventory")).json()
            assert len(inventory["devices"]) == 1
            device_id = inventory["devices"][0]["id"]
            reading = await client.get(
                f"/api/v1/onboarding/state/{device_id}", params={"connection_id": profile["id"]}
            )
        assert reading.status_code == 200
        assert reading.json()["read_status"] == "verified"
        state = reading.json()["states"][0]
        assert state["value"] == 21.5 and state["unit"] == "°C"
        from datetime import datetime

        assert datetime.fromisoformat(state["observed_at"]) == datetime.fromisoformat(
            source.states[0]["last_updated"]
        )
        assert inventory["coverage_evidence"]["rows"][0]["evidence_status"] == "not_verified"
        assert source.commands == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["cancel", "replace", "expiry", "capacity"])
async def test_temporary_setup_credentials_never_materialize_before_create(tmp_path, end):
    from datetime import UTC, datetime

    import httpx

    from domoai.management.api import create_management_app
    from domoai.security.secrets import SecretUnavailableError
    from tests.integration.test_connection_manager import _build_manager

    manager, database, _, store = await _build_manager(tmp_path)
    try:
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="first"
        )
        reference = setup.draft.credential_ref
        assert store.resolve(reference) == "first"
        assert list(store.secret_dir.glob("*.json")) == []
        assert await manager.list() == []
        if end == "cancel":
            app = create_management_app(manager)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                response = await client.post(
                    "/api/v1/connections/setup",
                    json={
                        "action": "cancel",
                        "session_id": str(session),
                    },
                )
            assert response.status_code == 200
        elif end == "replace":
            replacement = await manager.setup_draft(
                session, endpoint={}, options={}, credential_value="second"
            )
            assert store.resolve(replacement.draft.credential_ref) == "second"
        elif end == "expiry":
            manager.onboarding._sessions[session]["expires_at"] = datetime.now(UTC) - timedelta(
                seconds=1
            )
            with pytest.raises(ValueError, match="expired"):
                await manager.validate_setup(session)
        else:
            manager.onboarding.max_sessions = 1
            await manager.start_setup("fixture")
        with pytest.raises(SecretUnavailableError):
            store.resolve(reference)
        assert list(store.secret_dir.glob("*.json")) == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_borrowed_temporary_reference_cannot_be_adopted_by_another_operation(tmp_path):
    from domoai.application.connection_manager import ConnectionManagerError
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, database, _, store = await _build_manager(tmp_path)
    try:
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="first"
        )
        foreign = _setup()
        foreign = foreign.model_copy(
            update={
                "draft": foreign.draft.model_copy(
                    update={
                        "credential_ref": setup.draft.credential_ref,
                    }
                )
            }
        )
        with pytest.raises(ConnectionManagerError) as caught:
            await manager.create(foreign, activate=False)
        assert caught.value.code == "invalid_credentials"
        assert store.resolve(setup.draft.credential_ref) == "first"
        assert await manager.list() == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_invalid_setup_draft_leaves_no_resolvable_temporary_credential(tmp_path, monkeypatch):
    from pydantic import ValidationError

    from domoai.security.secrets import SecretUnavailableError
    from tests.integration.test_connection_manager import _build_manager

    manager, database, _, store = await _build_manager(tmp_path)
    references = []
    original = store.put_temporary

    def observe(value, *, owner):
        reference = original(value, owner=owner)
        references.append(reference)
        return reference

    monkeypatch.setattr(store, "put_temporary", observe)
    try:
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        with pytest.raises(ValidationError):
            await manager.setup_draft(
                session,
                endpoint={},
                options={},
                mapping_ref="",
                credential_value="invalid-draft-secret",
            )
        for reference in references:
            with pytest.raises(SecretUnavailableError):
                store.resolve(reference)
        assert list(store.secret_dir.glob("*.json")) == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_reauth_cannot_borrow_foreign_session_temporary_before_pointer_write(tmp_path):
    from domoai.application.connection_manager import ConnectionManagerError
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, database, _, store = await _build_manager(tmp_path)
    try:
        profile = await manager.create(_setup(), activate=False)
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="first"
        )
        with pytest.raises(ConnectionManagerError) as caught:
            await manager.reauth_ref(profile.id, setup.draft.credential_ref)
        assert caught.value.code == "invalid_credentials"
        assert (await manager.get(profile.id)).credential_ref is None
        assert store.resolve(setup.draft.credential_ref) == "first"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_value_on_legacy_secret_port_fails_before_persistence_or_secret_write(tmp_path):
    from domoai.application.connection_manager import ConnectionManagerError
    from tests.integration.test_connection_manager import _build_manager, _setup

    class LegacyStore:
        puts = 0

        def put(self, value):
            self.puts += 1
            return "env:FIXTURE"

        def resolve(self, reference):
            return "fixture"

        def delete(self, reference):
            raise AssertionError("borrowed legacy store must not be mutated")

    manager, database, _, _ = await _build_manager(tmp_path)
    store = LegacyStore()
    manager.secret_store = store
    try:
        with pytest.raises(ConnectionManagerError) as caught:
            await manager.create(_setup(), credential_value="fixture", activate=False)
        assert caught.value.code == "temporary_credentials_unsupported"
        assert store.puts == 0
        assert await manager.list() == []
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_pending_is_committed_with_exact_envelope_hash_before_materialization(
    tmp_path, backend, monkeypatch
):
    import hashlib
    import json

    from domoai.application.connection_manager import ConnectionManager
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.security.secrets import FileSecretStore
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, _ = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge")
    store = FileSecretStore(
        tmp_path / "scoped.key", secret_dir=tmp_path / "scoped-secrets", scope=scope
    )
    async with connection_databases(tmp_path, backend) as (database, observer):
        manager = ConnectionManager(
            repository=ConnectionRepository(database, scope=scope),
            providers=prototype.providers,
            runtime=prototype.runtime,
            secret_store=store,
        )
        write = store._atomic_write
        observations = []

        def observed_write(path, payload):
            rows = observer.connection.execute(
                "SELECT secret_ref, envelope_digest, state, tenant_id, household_id, deployment_id "
                "FROM connection_secret_transfers"
            ).fetchall()
            observer.connection.commit()
            assert len(rows) == 1, "pending must commit before any ciphertext file exists"
            row = tuple(rows[0])
            expected = hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            assert row == ("secret:" + path.stem, expected, "pending", "tenant", "home", "edge")
            assert not path.exists()
            observations.append(row)
            write(path, payload)

        monkeypatch.setattr(store, "_atomic_write", observed_write)
        profile = await manager.create(_setup(), credential_value="owned", activate=False)
        assert len(observations) == 1
        assert store.resolve(profile.credential_ref) == "owned"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("fail_adoption", [False, True])
@pytest.mark.parametrize("operation", ["create", "reauth"])
async def test_profile_ownership_and_transfer_adoption_commit_or_rollback_together(
    tmp_path, backend, fail_adoption, operation
):
    from domoai.application.connection_manager import ConnectionManager
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.security.secrets import FileSecretStore
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, _ = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge")
    store = FileSecretStore(
        tmp_path / "scoped.key", secret_dir=tmp_path / "scoped-secrets", scope=scope
    )
    async with connection_databases(tmp_path, backend) as (database, observer):
        manager = ConnectionManager(
            repository=ConnectionRepository(database, scope=scope),
            providers=prototype.providers,
            runtime=prototype.runtime,
            secret_store=store,
        )
        previous = None
        if operation == "reauth":
            setup = _setup()
            old_ref = store.put("previous")
            setup = setup.model_copy(
                update={"draft": setup.draft.model_copy(update={"credential_ref": old_ref})}
            )
            previous = await manager.create(setup, activate=False)

        async def perform():
            if previous is not None:
                return await manager.reauth(previous.id, "owned")
            return await manager.create(_setup(), credential_value="owned", activate=False)

        if fail_adoption:
            if backend == "sqlite":
                database.connection.execute("""CREATE TRIGGER reject_adoption
                    BEFORE UPDATE ON connection_secret_transfers WHEN NEW.state = 'adopted'
                    BEGIN SELECT RAISE(ABORT, 'fixture adoption failure'); END""")
            else:
                database.connection.execute("""CREATE FUNCTION reject_adoption()
                    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.state = 'adopted'
                    THEN RAISE EXCEPTION 'fixture adoption failure'; END IF; RETURN NEW; END $$""")
                database.connection.execute("""CREATE TRIGGER reject_adoption
                    BEFORE UPDATE ON connection_secret_transfers
                    FOR EACH ROW EXECUTE FUNCTION reject_adoption()""")
            database.connection.commit()
            with pytest.raises(Exception, match="fixture adoption failure"):
                await perform()
        else:
            profile = await perform()
        profiles = observer.connection.execute(
            "SELECT id, credential_ref FROM connections"
        ).fetchall()
        ownership = observer.connection.execute(
            "SELECT connection_id FROM connection_ownership"
        ).fetchall()
        transfers = observer.connection.execute(
            "SELECT profile_id, secret_ref, state FROM connection_secret_transfers"
        ).fetchall()
        observer.connection.commit()
        if fail_adoption:
            assert list(map(tuple, profiles)) == ([(str(previous.id), old_ref)] if previous else [])
            assert list(map(tuple, ownership)) == ([(str(previous.id),)] if previous else [])
            assert len(transfers) == 1 and transfers[0][2] in {"pending", "discarded"}
        else:
            assert list(map(tuple, profiles)) == [(str(profile.id), profile.credential_ref)]
            assert list(map(tuple, ownership)) == [(str(profile.id),)]
            assert list(map(tuple, transfers)) == [
                (str(profile.id), profile.credential_ref, "adopted")
            ]
        if previous is not None:
            assert store.resolve(old_ref) == "previous"


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("conflict", ["old_ref", "identity", "scope"])
async def test_reauth_transfer_cas_rejects_stale_reference_identity_and_scope(
    tmp_path, backend, conflict
):
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager.repository = repository
        old_ref = store.put("old")
        setup = _setup()
        setup = setup.model_copy(
            update={"draft": setup.draft.model_copy(update={"credential_ref": old_ref})}
        )
        original = await manager.create(setup, activate=False)
        owner = object()
        new_ref = store.put_temporary("new", owner=owner)
        prepared = store.prepare_temporary(new_ref, owner=owner)
        updated = original.model_copy(update={"credential_ref": new_ref})
        if conflict == "identity":
            updated = updated.model_copy(update={"unique_id": "other-identity"})
        if conflict == "scope":
            repository = ConnectionRepository(
                database, scope=scope.model_copy(update={"household_id": "other"})
            )
        transfer = SecretTransfer(
            uuid4(),
            new_ref,
            original.id,
            updated.connector_id,
            updated.unique_id,
            prepared.digest,
            operation="reauth",
            expected_old_ref="secret:" + str(uuid4()) if conflict == "old_ref" else old_ref,
        )
        await repository.prepare_secret_transfer(transfer)
        store.materialize(prepared)
        with pytest.raises(ValueError):
            await repository.adopt_secret_transfer(updated, transfer.transfer_id)
        row = observer.connection.execute(
            "SELECT credential_ref FROM connections WHERE id = ?", (str(original.id),)
        ).fetchone()
        state = observer.connection.execute(
            "SELECT state FROM connection_secret_transfers WHERE transfer_id = ?",
            (str(transfer.transfer_id),),
        ).fetchone()
        observer.connection.commit()
        assert row[0] == old_ref and state[0] == "pending"
        assert store.resolve(old_ref) == "old"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["withdrawn", "other_connection", "stale", "empty"])
async def test_cached_read_does_not_verify_absent_or_mismatched_source(tmp_path, failure):
    async with home_assistant_http_source() as source, onboarding_gateway(tmp_path) as gateway:
        profile = await connect_home_assistant(gateway, source)
        runtime = gateway.runtime
        # Stop automatic acquisition so the source/cache boundary is deterministic.
        await runtime.lifecycle.close()
        device = runtime.registry.devices[0]
        connection_id = profile["id"]
        if failure == "withdrawn":
            await runtime.adapter.detach("connection:" + profile["id"])
        elif failure == "other_connection":
            second = await runtime.connection_manager.repository.get(UUID(profile["id"]))
            from uuid import uuid4

            second = second.model_copy(update={"id": uuid4(), "unique_id": "other-ha"})
            await runtime.connection_manager.repository.save(second)
            connection_id = str(second.id)
        else:
            for state in await runtime.state_store.all():
                if failure == "stale":
                    old = runtime.clock.now() - timedelta(days=1)
                    await runtime.state_store.save(
                        state.model_copy(
                            update={
                                "status": StateStatus.STALE,
                                "observed_at": old,
                                "received_at": runtime.clock.now(),
                            }
                        )
                    )
                    assert (await runtime.state_store.all())[0].status is StateStatus.STALE
                else:
                    # A visible capability with no sample is a valid empty-cache boundary.
                    await runtime.state_store.delete(device.id)
                    assert await runtime.state_store.all() == []
        revision = runtime.state_store.runtime_revision
        async with gateway.http_client() as client:
            response = await client.get(
                f"/api/v1/onboarding/state/{device.id}", params={"connection_id": connection_id}
            )
        assert response.status_code == 200
        assert response.json()["read_status"] == "pending"
        assert response.json()["diagnostics"]
        assert runtime.state_store.runtime_revision == revision
        assert source.commands == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("operation", ["create", "reauth"])
@pytest.mark.parametrize("outcome", ["readback", "unknown_timeout", "unknown_query", "cancel"])
async def test_delayed_real_commit_preserves_secret_uuid_and_cancel_semantics(
    tmp_path, backend, operation, outcome, monkeypatch
):
    import asyncio
    import threading

    from domoai.application.connection_manager import ConnectionManager, ConnectionManagerError
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    committed, release = threading.Event(), threading.Event()
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager = ConnectionManager(
            repository=repository,
            providers=prototype.providers,
            runtime=prototype.runtime,
            secret_store=store,
        )
        original = None
        if operation == "reauth":
            setup = _setup()
            old_ref = store.put("old")
            setup = setup.model_copy(
                update={"draft": setup.draft.model_copy(update={"credential_ref": old_ref})}
            )
            original = await manager.create(setup, activate=False)
        raw = database.connection

        class DelayedCommit:
            adopting = False

            def __getattr__(self, name):
                return getattr(raw, name)

            def execute(self, sql, *args):
                if "SET state = 'adopted'" in sql:
                    self.adopting = True
                return raw.execute(sql, *args)

            def commit(self):
                delayed, self.adopting = self.adopting, False
                raw.commit()
                if delayed:
                    committed.set()
                    assert release.wait(5), "test did not release the real committed worker"

        attribute = "_proxy" if backend == "postgres" else "_connection"
        previous_connection = getattr(database, attribute)
        setattr(database, attribute, DelayedCommit())
        executor = SerializedStorageExecutor(operation_timeout_seconds=0.05)
        query_allowed = False
        original_query = repository.get_secret_transfer

        async def query(transfer_id):
            if outcome == "unknown_query" and committed.is_set() and not query_allowed:
                raise RuntimeError("synthetic unavailable readback")
            return await original_query(transfer_id)

        monkeypatch.setattr(repository, "get_secret_transfer", query)
        manager.repository = SerializedRepositoryProxy(repository, executor)
        task = asyncio.create_task(
            manager.reauth(original.id, "new")
            if original
            else manager.create(_setup(), credential_value="new", activate=False)
        )
        try:
            assert await asyncio.to_thread(committed.wait, 3)
            if outcome == "cancel":
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                for _ in range(200):
                    if executor.metrics.timeout_count:
                        break
                    await asyncio.sleep(0.005)
                assert executor.metrics.timeout_count >= 1
                if outcome != "unknown_timeout":
                    release.set()
                if outcome == "readback":
                    result = await asyncio.wait_for(task, 2)
                    assert result.enabled is False
                else:
                    with pytest.raises(ConnectionManagerError) as caught:
                        await asyncio.wait_for(task, 2)
                    assert caught.value.code == "connection_persistence_pending"
            rows = observer.connection.execute(
                "SELECT id, credential_ref FROM connections"
            ).fetchall()
            transfers = observer.connection.execute(
                "SELECT state FROM connection_secret_transfers"
            ).fetchall()
            observer.connection.commit()
            assert len(rows) == 1 and transfers[0][0] == "adopted"
            identity, reference = rows[0]
            assert store.resolve(reference) == "new"
            assert (store.secret_dir / (reference.removeprefix("secret:") + ".json")).is_file()
            if original is not None:
                assert identity == str(original.id)
                assert store.resolve(old_ref) == "old"
            if outcome != "readback":
                assert store.is_temporary(reference), "unknown/cancel must retain the pin"
            if outcome == "unknown_query":
                import httpx

                from domoai.management.api import create_management_app

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(
                        app=create_management_app(manager), raise_app_exceptions=False
                    ),
                    base_url="http://localhost",
                ) as client:
                    pending = await client.get("/api/v1/connections")
                assert pending.status_code == 409
                assert pending.json()["error"] == "connection_persistence_pending"
                assert pending.headers["cache-control"] == "no-store"
                assert "new" not in pending.text and "secret:" not in pending.text
            release.set()
            await executor.run(lambda: None)
            query_allowed = True
            import httpx

            from domoai.management.api import create_management_app

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_management_app(manager)),
                base_url="http://localhost",
            ) as client:
                listed = await client.get("/api/v1/connections")
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()["connections"]] == [identity]
            assert not store.is_temporary(reference), "HTTP list reconciles its own confirmed pin"
            recovered = await manager.get(UUID(identity))
            assert recovered.id == UUID(identity) and recovered.credential_ref == reference
            assert not store.is_temporary(reference), "confirmed adoption releases only its own pin"
            assert manager.runtime.adapters == ()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await executor.close()
            setattr(database, attribute, previous_connection)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("failure", ["before_file", "after_file", "adoption", "collision"])
async def test_known_failed_owned_transfer_discards_only_matching_unreferenced_file(
    tmp_path, backend, failure, monkeypatch
):
    import hashlib
    from pathlib import Path

    from domoai.application.connection_manager import ConnectionManager
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.security.secrets import SecretStoreError
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager = ConnectionManager(
            repository=repository,
            providers=prototype.providers,
            runtime=prototype.runtime,
            secret_store=store,
        )
        write = store._atomic_write
        materialize = store.materialize
        unlink = Path.unlink
        removals = []

        def observed_unlink(path, *args, **kwargs):
            if path.parent == store.secret_dir and path.suffix == ".json":
                row = observer.connection.execute(
                    "SELECT state, envelope_digest FROM connection_secret_transfers "
                    "WHERE secret_ref = ?",
                    ("secret:" + path.stem,),
                ).fetchone()
                uses = observer.connection.execute(
                    "SELECT COUNT(*) FROM connections WHERE credential_ref = ?",
                    ("secret:" + path.stem,),
                ).fetchone()[0]
                observer.connection.commit()
                assert row[0] == "discarding" and uses == 0
                assert hashlib.sha256(path.read_bytes()).hexdigest() == row[1]
                removals.append(path.name)
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", observed_unlink)

        def failing_write(path, payload):
            if failure == "after_file":
                write(path, payload)
            raise SecretStoreError("fixture materialization failure")

        def conflicting_materialize(prepared):
            store.secret_dir.mkdir(exist_ok=True)
            path = store.secret_dir / (prepared.reference.removeprefix("secret:") + ".json")
            path.write_bytes(b"foreign-ciphertext-do-not-delete")
            path.chmod(0o600)
            materialize(prepared)

        if failure in {"before_file", "after_file"}:
            monkeypatch.setattr(store, "_atomic_write", failing_write)
        elif failure == "collision":
            monkeypatch.setattr(store, "materialize", conflicting_materialize)
        else:
            if backend == "sqlite":
                database.connection.execute("""CREATE TRIGGER reject_adoption
                    BEFORE UPDATE ON connection_secret_transfers WHEN NEW.state = 'adopted'
                    BEGIN SELECT RAISE(ABORT, 'fixture adoption failure'); END""")
            else:
                database.connection.execute("""CREATE FUNCTION reject_adoption()
                    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.state = 'adopted'
                    THEN RAISE EXCEPTION 'fixture adoption failure'; END IF; RETURN NEW; END $$""")
                database.connection.execute("""CREATE TRIGGER reject_adoption
                    BEFORE UPDATE ON connection_secret_transfers
                    FOR EACH ROW EXECUTE FUNCTION reject_adoption()""")
            database.connection.commit()
        with pytest.raises(Exception, match="fixture|conflict|recovery"):
            await manager.create(_setup(), credential_value="owned", activate=False)
        rows = observer.connection.execute(
            "SELECT secret_ref, state FROM connection_secret_transfers"
        ).fetchall()
        profiles = observer.connection.execute("SELECT id FROM connections").fetchall()
        observer.connection.commit()
        assert len(rows) == 1 and profiles == []
        reference, state = rows[0]
        files = list(store.secret_dir.glob("*.json"))
        if failure == "collision":
            assert state == "discarding"
            assert len(files) == 1 and files[0].read_bytes() == b"foreign-ciphertext-do-not-delete"
            assert removals == []
        else:
            assert state == "discarded"
            assert files == []
            assert len(removals) == (0 if failure == "before_file" else 1)
            assert not store.is_temporary(reference)


@pytest.mark.asyncio
async def test_cancel_setup_after_pending_commit_stops_materialization_and_discards_own_row(
    tmp_path, monkeypatch
):
    import asyncio

    from tests.integration.test_connection_manager import _build_manager

    manager, database, _, store = await _build_manager(tmp_path)
    committed, release = asyncio.Event(), asyncio.Event()
    original_prepare = manager.repository.prepare_secret_transfer

    async def paused_prepare(transfer):
        await original_prepare(transfer)
        committed.set()
        await release.wait()

    monkeypatch.setattr(manager.repository, "prepare_secret_transfer", paused_prepare)
    task = None
    try:
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="owned"
        )
        task = asyncio.create_task(manager.create_setup(session, activate=False))
        await asyncio.wait_for(committed.wait(), 2)
        assert store.is_temporary(setup.draft.credential_ref)
        assert list(store.secret_dir.glob("*.json")) == []
        assert await manager.cancel_setup(session)
        assert store.is_temporary(setup.draft.credential_ref), (
            "in-flight pin must survive cancellation"
        )
        release.set()
        with pytest.raises(Exception, match="temporary|cancel"):
            await task
        row = database.connection.execute(
            "SELECT state FROM connection_secret_transfers"
        ).fetchone()
        assert row[0] == "discarded"
        assert await manager.list() == []
        assert list(store.secret_dir.glob("*.json")) == []
        assert not store.is_temporary(setup.draft.credential_ref)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_two_creates_of_one_session_cannot_release_each_others_live_pin(
    tmp_path, monkeypatch
):
    import asyncio

    from tests.integration.test_connection_manager import _build_manager

    manager, database, _, store = await _build_manager(tmp_path)
    committed, release = asyncio.Event(), asyncio.Event()
    prepare = manager.repository.prepare_secret_transfer
    calls = 0

    async def paused_prepare(transfer):
        nonlocal calls
        calls += 1
        await prepare(transfer)
        if calls == 1:
            committed.set()
            await release.wait()

    monkeypatch.setattr(manager.repository, "prepare_secret_transfer", paused_prepare)
    tasks = []
    try:
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="owned"
        )
        tasks.append(asyncio.create_task(manager.create_setup(session, activate=False)))
        await asyncio.wait_for(committed.wait(), 2)
        tasks.append(asyncio.create_task(manager.create_setup(session, activate=False)))
        await asyncio.sleep(0.03)
        assert store.is_temporary(setup.draft.credential_ref), "second request stole first live pin"
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 3)
        assert any(not isinstance(result, BaseException) for result in results)
        profiles = await manager.list()
        assert len(profiles) == 1
        assert profiles[0].credential_ref == setup.draft.credential_ref
        assert store.resolve(profiles[0].credential_ref) == "owned"
        assert len(list(store.secret_dir.glob("*.json"))) == 1
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("mutation", ["save", "set_ref", "update"])
async def test_repository_cannot_add_borrowed_reference_after_discard_admission(
    tmp_path, backend, mutation
):
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager.repository = repository
        profile = await manager.create(_setup(), activate=False)
        owner = object()
        reference = store.put_temporary("owned", owner=owner)
        prepared = store.prepare_temporary(reference, owner=owner)
        transfer = SecretTransfer(
            uuid4(), reference, uuid4(), "fixture", "reserved", prepared.digest
        )
        await repository.prepare_secret_transfer(transfer)
        store.materialize(prepared)
        database.connection.execute("UPDATE connection_secret_transfers SET state = 'discarding'")
        database.connection.commit()
        foreign = ConnectionRepository(
            database, scope=scope.model_copy(update={"household_id": "other"})
        )
        if mutation == "save":
            candidate = profile.model_copy(
                update={"id": uuid4(), "unique_id": "foreign", "credential_ref": reference}
            )
            with pytest.raises(ValueError, match="credential"):
                await foreign.save(candidate)
        elif mutation == "set_ref":
            with pytest.raises(ValueError, match="credential"):
                await repository.set_credential_ref(profile.id, reference)
        else:
            with pytest.raises(ValueError, match="credential"):
                await repository.update(profile.model_copy(update={"credential_ref": reference}))
        assert (
            observer.connection.execute(
                "SELECT COUNT(*) FROM connections WHERE credential_ref = ?", (reference,)
            ).fetchone()[0]
            == 0
        )
        observer.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("failure", ["before_unlink", "after_unlink", "discard_response"])
async def test_current_instance_retries_discard_without_recreating_or_overdeleting(
    tmp_path, backend, failure, monkeypatch
):
    from domoai.application.connection_manager import ConnectionManager, ConnectionManagerError
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager = ConnectionManager(
            repository=repository,
            providers=prototype.providers,
            runtime=prototype.runtime,
            secret_store=store,
        )

        async def failed_adoption(*args):
            raise ValueError("fixture known failure")

        monkeypatch.setattr(repository, "adopt_secret_transfer", failed_adoption)
        discard = store.discard_prepared
        finish = repository.finish_secret_discard
        attempts = 0

        def interrupted_discard(prepared):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                if failure == "after_unlink":
                    assert discard(prepared)
                raise OSError("fixture unlink boundary")
            return discard(prepared)

        async def interrupted_finish(transfer):
            nonlocal attempts
            attempts += 1
            result = await finish(transfer)
            if attempts == 1:
                raise RuntimeError("fixture lost discard response")
            return result

        if failure == "discard_response":
            monkeypatch.setattr(repository, "finish_secret_discard", interrupted_finish)
        else:
            monkeypatch.setattr(store, "discard_prepared", interrupted_discard)
        with pytest.raises(ConnectionManagerError) as caught:
            await manager.create(_setup(), credential_value="owned", activate=False)
        assert caught.value.code == "connection_persistence_pending"
        reference, state = observer.connection.execute(
            "SELECT secret_ref, state FROM connection_secret_transfers"
        ).fetchone()
        observer.connection.commit()
        assert state == ("discarded" if failure == "discard_response" else "discarding")
        assert store.is_temporary(reference)
        assert len(list(store.secret_dir.glob("*.json"))) == (
            1 if failure == "before_unlink" else 0
        )
        assert await manager.list() == []
        assert (
            observer.connection.execute("SELECT state FROM connection_secret_transfers").fetchone()[
                0
            ]
            == "discarded"
        )
        observer.connection.commit()
        assert not store.is_temporary(reference)
        assert list(store.secret_dir.glob("*.json")) == []
        assert await manager.list() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("veto", ["foreign_reference", "missing_row"])
async def test_current_cleanup_preserves_file_when_any_household_references_it_or_row_is_missing(
    tmp_path, backend, veto, monkeypatch
):
    from uuid import uuid4

    from domoai.application.connection_manager import ConnectionManager, ConnectionManagerError
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import (
        _SELECT_COLUMNS,
        ConnectionRepository,
        _serialize_profile,
    )
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager = ConnectionManager(
            repository=repository,
            providers=prototype.providers,
            runtime=prototype.runtime,
            secret_store=store,
        )

        async def unexpected_legacy_reference(profile, transfer_id):
            if veto == "foreign_reference":
                foreign = profile.model_copy(update={"id": uuid4(), "unique_id": "foreign"})
                # Simulate pre-protocol external data. The ordinary repository
                # now rejects this reference; every persisted use still vetoes cleanup.
                database.connection.execute(
                    f"INSERT INTO connections ({_SELECT_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _serialize_profile(foreign),
                )
                database.connection.execute(
                    "INSERT INTO connection_ownership VALUES (?, ?, ?, ?, ?)",
                    (
                        str(foreign.id),
                        "foreign-tenant",
                        "foreign-home",
                        "edge",
                        profile.created_at.isoformat(),
                    ),
                )
            else:
                database.connection.execute(
                    "DELETE FROM connection_secret_transfers WHERE transfer_id = ?",
                    (str(transfer_id),),
                )
            database.connection.commit()
            raise ValueError("fixture known failure")

        monkeypatch.setattr(repository, "adopt_secret_transfer", unexpected_legacy_reference)
        with pytest.raises(ConnectionManagerError) as caught:
            await manager.create(_setup(), credential_value="owned", activate=False)
        assert caught.value.code == "connection_recovery_pending"
        files = list(store.secret_dir.glob("*.json"))
        assert len(files) == 1
        original = files[0].read_bytes()
        with pytest.raises(ConnectionManagerError):
            await manager.list()
        assert files[0].read_bytes() == original
        assert len(manager._owned_transfers) == 1
        if veto == "foreign_reference":
            assert (
                observer.connection.execute(
                    "SELECT state FROM connection_secret_transfers"
                ).fetchone()[0]
                == "pending"
            )
            observer.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("order", ["attach_first", "discard_first", "overlap"])
@pytest.mark.parametrize("mutation", ["save", "set_ref", "update"])
async def test_reference_attachment_and_discard_admission_use_one_transactional_order(
    tmp_path, backend, order, mutation
):
    import asyncio
    import threading
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (writer, discarder):
        first = ConnectionRepository(writer, scope=scope)
        second = ConnectionRepository(discarder, scope=scope)
        manager.repository = first
        profile = await manager.create(_setup(), activate=False)
        owner = object()
        reference = store.put_temporary("owned", owner=owner)
        prepared = store.prepare_temporary(reference, owner=owner)
        transfer = SecretTransfer(
            uuid4(), reference, uuid4(), "fixture", "reserved", prepared.digest
        )
        await first.prepare_secret_transfer(transfer)
        store.materialize(prepared)

        async def attach():
            try:
                if mutation == "save":
                    await first.save(
                        profile.model_copy(
                            update={"id": uuid4(), "unique_id": "new", "credential_ref": reference}
                        )
                    )
                elif mutation == "set_ref":
                    await first.set_credential_ref(profile.id, reference)
                else:
                    await first.update(profile.model_copy(update={"credential_ref": reference}))
            except ValueError:
                return False
            return True

        async def discard():
            return await second.begin_secret_discard(transfer)

        if order == "attach_first":
            attached, discarded = await attach(), await discard()
        elif order == "discard_first":
            discarded, attached = await discard(), await attach()
        else:
            barrier = threading.Barrier(2)

            def start(operation):
                barrier.wait(timeout=3)
                return asyncio.run(operation())

            attached, discarded = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(start, attach),
                    asyncio.to_thread(start, discard),
                ),
                10,
            )
        assert attached is False and discarded is True
        assert (
            writer.connection.execute(
                "SELECT COUNT(*) FROM connections WHERE credential_ref = ?", (reference,)
            ).fetchone()[0]
            == 0
        )
        assert (
            writer.connection.execute("SELECT state FROM connection_secret_transfers").fetchone()[0]
            == "discarding"
        )
        writer.connection.commit()
        assert store.matches_secret(reference, prepared.digest)


@pytest.mark.asyncio
async def test_concurrent_activation_keeps_one_acquired_child_without_loser_compensation(tmp_path):
    import asyncio

    from tests.integration.test_connection_manager import FakeAdapter, _build_manager, _setup

    manager, database, _, _ = await _build_manager(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    built = []

    class PausedAdapter(FakeAdapter):
        async def connect(self):
            started.set()
            await release.wait()
            await super().connect()

    def factory():
        child = PausedAdapter()
        built.append(child)
        return child

    manager.providers.get("fixture")._adapter_factory = factory
    try:
        profile = await manager.create(_setup(), activate=False)
        first = asyncio.create_task(manager.activate(profile.id))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(manager.activate(profile.id))
        await asyncio.sleep(0.02)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), 3)
        assert all(getattr(result, "id", None) == profile.id for result in results)
        assert len(built) == 1
        assert manager.runtime.adapters == (built[0],) and built[0].connected
        assert (await manager.get(profile.id)).enabled
    finally:
        release.set()
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["repair", "reauth", "reauth_ref"])
async def test_enabled_profile_without_runtime_child_recovers_same_uuid(tmp_path, operation):
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, database, _, store = await _build_manager(tmp_path)
    try:
        profile = await manager.create(_setup(), credential_value="old")
        await manager.runtime.detach(f"connection:{profile.id}")
        assert (await manager.get(profile.id)).enabled and manager.runtime.adapters == ()
        if operation == "repair":
            result = await manager.repair(profile.id)
        elif operation == "reauth":
            result = await manager.reauth(profile.id, "new")
        else:
            result = await manager.reauth_ref(profile.id, store.put("new"))
        assert result.id == profile.id and result.enabled
        assert len(manager.runtime.adapters) == 1
        assert manager.runtime.adapters[0].connected
        assert store.resolve(profile.credential_ref) == "old"
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reauth", "reauth_ref"])
async def test_failed_runtime_reauth_preserves_committed_pointer_and_both_credentials(
    tmp_path, operation
):
    from domoai.application.connection_manager import ConnectionManagerError
    from tests.integration.test_connection_manager import FakeAdapter, _build_manager, _setup

    manager, database, _, store = await _build_manager(tmp_path)
    try:
        profile = await manager.create(_setup(), credential_value="old")
        original_child = manager.runtime.adapters[0]
        manager.providers.get("fixture")._adapter_factory = lambda: FakeAdapter(fail=True)
        supplied = store.put("new") if operation == "reauth_ref" else None
        with pytest.raises(ConnectionManagerError) as caught:
            if supplied is None:
                await manager.reauth(profile.id, "new")
            else:
                await manager.reauth_ref(profile.id, supplied)
        current = await manager.repository.get(profile.id)
        assert current.credential_ref != profile.credential_ref
        assert store.resolve(current.credential_ref) == "new"
        assert store.resolve(profile.credential_ref) == "old"
        assert caught.value.code == "connection_recovery_pending"
        assert manager.runtime.adapters == (original_child,) and original_child.connected
        manager.providers.get("fixture")._adapter_factory = FakeAdapter
        repaired = await manager.repair(profile.id)
        assert repaired.id == profile.id and store.resolve(repaired.credential_ref) == "new"
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("borrowed", [False, True])
async def test_delete_profile_preserves_adopted_and_supplied_secret_history(tmp_path, borrowed):
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, database, _, store = await _build_manager(tmp_path)
    try:
        if borrowed:
            reference = store.put("borrowed")
            setup = _setup()
            setup = type(setup)(
                candidate=setup.candidate,
                draft=setup.draft.model_copy(update={"credential_ref": reference}),
            )
            profile = await manager.create(setup, activate=False)
        else:
            profile = await manager.create(_setup(), credential_value="adopted", activate=False)
        rows = database.connection.execute("SELECT * FROM connection_secret_transfers").fetchall()
        assert await manager.delete(profile.id)
        assert await manager.repository.get(profile.id) is None
        assert store.resolve(profile.credential_ref) == ("borrowed" if borrowed else "adopted")
        assert (
            database.connection.execute("SELECT * FROM connection_secret_transfers").fetchall()
            == rows
        )
        assert not await manager.delete(profile.id)
    finally:
        await manager.runtime.disconnect()
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("mismatch", [None, "reference", "identity", "scope"])
async def test_borrowed_reauth_cas_preserves_profile_scope_identity_and_history(
    tmp_path, backend, mismatch
):
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from tests.integration.test_connection_manager import _build_manager, _setup

    prototype, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        prototype.repository = repository
        profile = await prototype.create(_setup(), credential_value="old", activate=False)
        replacement = store.put("new")
        updated = profile.model_copy(update={"credential_ref": replacement})
        expected = profile.credential_ref
        if mismatch == "reference":
            expected = None
        elif mismatch == "identity":
            updated = updated.model_copy(update={"unique_id": "wrong"})
        elif mismatch == "scope":
            repository = ConnectionRepository(
                database,
                scope=LeaseScope(
                    tenant_id="default", household_id="other", deployment_id="default"
                ),
            )
        before = [
            tuple(row)
            for row in observer.connection.execute(
                "SELECT * FROM connection_secret_transfers"
            ).fetchall()
        ]
        observer.connection.commit()
        changed = await repository.compare_and_set_credential_ref(updated, expected)
        current = await prototype.repository.get(profile.id)
        assert changed is (mismatch is None)
        assert current.credential_ref == (
            replacement if mismatch is None else profile.credential_ref
        )
        assert store.resolve(profile.credential_ref) == "old"
        assert store.resolve(replacement) == "new"
        assert [
            tuple(row)
            for row in observer.connection.execute(
                "SELECT * FROM connection_secret_transfers"
            ).fetchall()
        ] == before
        observer.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("outcome", ["readback", "unknown_timeout", "unknown_query", "cancel"])
async def test_borrowed_reauth_real_lost_commit_response_preserves_both_refs(
    tmp_path, backend, outcome, monkeypatch
):
    import asyncio
    import threading

    from domoai.application.connection_manager import ConnectionManagerError
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    committed, release = threading.Event(), threading.Event()
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager.repository = repository
        profile = await manager.create(_setup(), credential_value="old", activate=False)
        supplied = store.put("new")
        raw = database.connection

        class DelayedCommit:
            updating = False

            def __getattr__(self, name):
                return getattr(raw, name)

            def execute(self, sql, *args):
                if "UPDATE connections" in sql and "credential_ref =" in sql:
                    self.updating = True
                return raw.execute(sql, *args)

            def commit(self):
                delay, self.updating = self.updating, False
                raw.commit()
                if delay:
                    committed.set()
                    assert release.wait(5), "fixture did not release committed worker"

        attribute = "_proxy" if backend == "postgres" else "_connection"
        previous = getattr(database, attribute)
        setattr(database, attribute, DelayedCommit())
        executor = SerializedStorageExecutor(operation_timeout_seconds=0.05)
        get = repository.get
        allow_query = False

        async def guarded_get(connection_id):
            if outcome == "unknown_query" and committed.is_set() and not allow_query:
                raise RuntimeError("fixture query unavailable")
            return await get(connection_id)

        monkeypatch.setattr(repository, "get", guarded_get)
        manager.repository = SerializedRepositoryProxy(repository, executor)
        task = asyncio.create_task(manager.reauth_ref(profile.id, supplied))
        try:
            assert await asyncio.to_thread(committed.wait, 3)
            if outcome == "cancel":
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                for _ in range(200):
                    if executor.metrics.timeout_count:
                        break
                    await asyncio.sleep(0.005)
                assert executor.metrics.timeout_count
                if outcome != "unknown_timeout":
                    release.set()
                if outcome == "readback":
                    assert (await asyncio.wait_for(task, 2)).id == profile.id
                else:
                    with pytest.raises(ConnectionManagerError) as caught:
                        await asyncio.wait_for(task, 2)
                    assert caught.value.code == "connection_persistence_pending"
            assert manager.runtime.adapters == ()
            assert store.resolve(profile.credential_ref) == "old"
            assert store.resolve(supplied) == "new"
            release.set()
            allow_query = True
            recovered = await manager.list()
            assert len(recovered) == 1 and recovered[0].id == profile.id
            assert recovered[0].credential_ref == supplied and not recovered[0].enabled
            assert (
                observer.connection.execute(
                    "SELECT COUNT(*) FROM connection_secret_transfers"
                ).fetchone()[0]
                == 1
            )
            observer.connection.commit()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await executor.close()
            setattr(database, attribute, previous)


@pytest.mark.asyncio
async def test_sqlite_postgres_copy_preserves_all_transfer_states_and_checkpoint(tmp_path):
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
    from domoai.persistence.postgres_migration import migrate_sqlite_to_postgres
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, source, _, store = await _build_manager(tmp_path)
    profile = await manager.create(_setup(), credential_value="adopted", activate=False)
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    repository = ConnectionRepository(source, scope=scope)
    borrowed = profile.model_copy(update={"id": uuid4(), "unique_id": "borrowed-copy"})
    await repository.save(borrowed)
    connection_columns = (
        "id, connector_id, unique_id, endpoint_json, options_json, "
        "credential_ref, mapping_ref, enabled, schema_version, "
        "created_at, updated_at, credential_transfer_id"
    )
    connection_rows = [
        tuple(row)
        for row in source.connection.execute(
            f"SELECT {connection_columns} FROM connections ORDER BY id"
        ).fetchall()
    ]
    assert sorted(row[-1] is None for row in connection_rows) == [False, True]
    for state in ("pending", "discarding", "discarded"):
        owner = object()
        ref = store.put_temporary(state, owner=owner)
        prepared = store.prepare_temporary(ref, owner=owner)
        transfer = SecretTransfer(uuid4(), ref, uuid4(), "fixture", state, prepared.digest)
        await repository.prepare_secret_transfer(transfer)
        source.connection.execute(
            "UPDATE connection_secret_transfers SET state = ? WHERE transfer_id = ?",
            (state, str(transfer.transfer_id)),
        )
        source.connection.commit()
    before = [
        tuple(row)
        for row in source.connection.execute(
            "SELECT * FROM connection_secret_transfers ORDER BY transfer_id"
        ).fetchall()
    ]
    floor = tuple(source.connection.execute("SELECT * FROM runtime_contract_floor").fetchone())
    try:
        async with connection_databases(tmp_path, "postgres") as (destination, observer):
            report = await migrate_sqlite_to_postgres(source, destination)
            actual = [
                tuple(row)
                for row in observer.connection.execute(
                    "SELECT * FROM connection_secret_transfers ORDER BY transfer_id"
                ).fetchall()
            ]
            assert actual == before
            assert [
                tuple(row)
                for row in observer.connection.execute(
                    f"SELECT {connection_columns} FROM connections ORDER BY id"
                ).fetchall()
            ] == connection_rows
            assert report.table_counts["connection_secret_transfers"] == 4
            assert report.payload_digests["connection_secret_transfers"]
            assert (
                tuple(
                    observer.connection.execute("SELECT * FROM runtime_contract_floor").fetchone()
                )
                == floor
            )
            assert (
                await ConnectionRepository(destination, scope=scope).get(profile.id)
            ).id == profile.id
            observer.connection.commit()
            assert store.resolve(profile.credential_ref) == "adopted"
    finally:
        await source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("mutation", ["metadata", "set_ref", "cas", "duplicate_save"])
async def test_adoption_binding_is_atomic_and_cannot_be_cleared_by_same_reference_write(
    tmp_path, backend, mutation
):
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        manager.repository = repository
        profile = await manager.create(_setup(), credential_value="old", activate=False)
        row = observer.connection.execute(
            "SELECT credential_transfer_id FROM connections WHERE id = ?", (str(profile.id),)
        ).fetchone()
        observer.connection.commit()
        binding = row[0]
        assert binding is not None
        assert (
            observer.connection.execute(
                "SELECT state FROM connection_secret_transfers WHERE transfer_id = ?", (binding,)
            ).fetchone()[0]
            == "adopted"
        )
        observer.connection.commit()
        if mutation == "metadata":
            await repository.update(profile.model_copy(update={"options": {"label": "changed"}}))
        elif mutation == "set_ref":
            await repository.set_credential_ref(profile.id, profile.credential_ref)
        elif mutation == "cas":
            assert await repository.compare_and_set_credential_ref(profile, profile.credential_ref)
        else:
            with pytest.raises(Exception, match="UNIQUE|duplicate"):
                await repository.save(profile)
        assert (
            observer.connection.execute(
                "SELECT credential_transfer_id FROM connections WHERE id = ?", (str(profile.id),)
            ).fetchone()[0]
            == binding
        )
        observer.connection.commit()
        replacement = store.put("borrowed")
        await manager.reauth_ref(profile.id, replacement)
        assert (
            observer.connection.execute(
                "SELECT credential_transfer_id FROM connections WHERE id = ?", (str(profile.id),)
            ).fetchone()[0]
            is None
        )
        observer.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize(
    "scenario",
    [
        "shared",
        "deleted_donor",
        "reauth",
        "missing",
        "hash",
        "identity",
        "scope",
        "profile",
        "binding",
    ],
)
async def test_restart_distinguishes_borrowed_history_from_corrupt_current_adoption(
    tmp_path, backend, scenario
):
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from tests.fixtures.household_onboarding import restarted_connection_probe
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        manager.repository = ConnectionRepository(database, scope=scope)
        profile = await manager.create(_setup(), credential_value="original", activate=False)
        setup = _setup()
        setup = setup.model_copy(
            update={
                "draft": setup.draft.model_copy(
                    update={"unique_id": "borrower", "credential_ref": profile.credential_ref}
                )
            }
        )
        borrower = await manager.create(setup, activate=False)
        if scenario == "deleted_donor":
            await manager.delete(profile.id)
        elif scenario == "reauth":
            await manager.reauth(profile.id, "replacement")
        elif scenario in {"missing", "hash"}:
            path = store.secret_dir / (profile.credential_ref.removeprefix("secret:") + ".json")
            if scenario == "missing":
                path.unlink()
            else:
                path.write_bytes(b"synthetic invalid ciphertext")
        elif scenario in {"identity", "scope", "profile"}:
            column = {"identity": "unique_id", "scope": "household_id", "profile": "profile_id"}[
                scenario
            ]
            database.connection.execute(
                f"UPDATE connection_secret_transfers SET {column} = ?",
                (str(uuid4()) if scenario == "profile" else "wrong",),
            )
            database.connection.commit()
        elif scenario == "binding":
            database.connection.execute(
                "UPDATE connections SET credential_transfer_id = ? WHERE id = ?",
                (str(uuid4()), str(profile.id)),
            )
            database.connection.commit()
        before = {p.name: p.read_bytes() for p in store.secret_dir.glob("*.json")}
        history = [
            tuple(row)
            for row in observer.connection.execute(
                "SELECT * FROM connection_secret_transfers ORDER BY transfer_id"
            ).fetchall()
        ]
        observer.connection.commit()
        result = await restarted_connection_probe(database, backend, tmp_path)
        codes = [item["code"] for item in result["diagnostics"]]
        if scenario in {"shared", "deleted_donor", "reauth"}:
            assert codes == []
        else:
            assert "connection_recovery_pending" in codes
        assert borrower.id.hex in {UUID(item["id"]).hex for item in result["profiles"]}
        assert result["children"] == 0 and result["owned_transfers"] == 0
        assert {p.name: p.read_bytes() for p in store.secret_dir.glob("*.json")} == before
        assert [
            tuple(row)
            for row in observer.connection.execute(
                "SELECT * FROM connection_secret_transfers ORDER BY transfer_id"
            ).fetchall()
        ] == history
        observer.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_same_session_retry_after_double_cancel_cannot_consume_original_pending_pin(
    tmp_path, backend
):
    import asyncio
    import threading

    from domoai.application.connection_manager import ConnectionManagerError
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
    from tests.integration.test_connection_manager import _build_manager

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    committed, release = threading.Event(), threading.Event()
    async with connection_databases(tmp_path, backend) as (database, observer):
        raw = database.connection

        class DelayedPendingCommit:
            pending = False

            def __getattr__(self, name):
                return getattr(raw, name)

            def execute(self, sql, *args):
                if "INSERT INTO connection_secret_transfers" in sql:
                    self.pending = True
                return raw.execute(sql, *args)

            def commit(self):
                delay, self.pending = self.pending, False
                raw.commit()
                if delay:
                    committed.set()
                    assert release.wait(5)

        attr = "_proxy" if backend == "postgres" else "_connection"
        previous = getattr(database, attr)
        setattr(database, attr, DelayedPendingCommit())
        executor = SerializedStorageExecutor(operation_timeout_seconds=0.2)
        scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
        manager.repository = SerializedRepositoryProxy(
            ConnectionRepository(database, scope=scope), executor
        )
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="owned"
        )
        task = asyncio.create_task(manager.create_setup(session, activate=False))
        try:
            assert await asyncio.to_thread(committed.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            retained = tuple(manager._owned_transfers)
            release.set()
            with pytest.raises(Exception) as caught:
                await manager.create_setup(session, activate=False)
            assert tuple(manager._owned_transfers) == retained
            assert store.is_temporary(setup.draft.credential_ref)
            assert isinstance(caught.value, ConnectionManagerError)
            assert caught.value.code == "connection_persistence_pending"
            assert (
                observer.connection.execute(
                    "SELECT COUNT(*) FROM connection_secret_transfers"
                ).fetchone()[0]
                == 1
            )
            observer.connection.commit()
            assert await manager.list() == []
            assert not store.is_temporary(setup.draft.credential_ref)
            assert manager.runtime.adapters == ()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await executor.close()
            setattr(database, attr, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("historical", ["pending", "discarding", "missing_database"])
async def test_old_database_copy_or_recreated_database_never_cleans_original_adopted_file(
    tmp_path, historical
):
    import sqlite3
    from uuid import uuid4

    from domoai.persistence.connection_repository import SecretTransfer
    from domoai.persistence.sqlite import SQLiteDatabase
    from tests.fixtures.household_onboarding import restarted_connection_probe
    from tests.integration.test_connection_manager import _build_manager
    from tests.unit.persistence.test_connection_repository import _profile

    manager, original, _, store = await _build_manager(tmp_path)
    owner = object()
    reference = store.put_temporary("original", owner=owner)
    prepared = store.prepare_temporary(reference, owner=owner)
    profile = _profile().model_copy(update={"credential_ref": reference})
    transfer = SecretTransfer(
        uuid4(), reference, profile.id, profile.connector_id, profile.unique_id, prepared.digest
    )
    await manager.repository.prepare_secret_transfer(transfer)
    clone = SQLiteDatabase(tmp_path / "copy.sqlite3")
    if historical != "missing_database":
        with sqlite3.connect(clone.path) as copy:
            original.connection.backup(copy)
        if historical == "discarding":
            with sqlite3.connect(clone.path) as copy:
                copy.execute("UPDATE connection_secret_transfers SET state = 'discarding'")
    store.materialize(prepared)
    await manager.repository.adopt_secret_transfer(profile, transfer.transfer_id)
    store.release_temporary(reference, owner=owner)
    before = {p.name: p.read_bytes() for p in store.secret_dir.glob("*.json")}
    try:
        result = await restarted_connection_probe(clone, "sqlite", tmp_path)
        assert result["profiles"] == [] and result["children"] == 0
        assert result["owned_transfers"] == 0
        assert {p.name: p.read_bytes() for p in store.secret_dir.glob("*.json")} == before
        assert store.resolve(reference) == "original"
        with sqlite3.connect(clone.path) as copy:
            rows = copy.execute("SELECT state FROM connection_secret_transfers").fetchall()
        assert rows == ([] if historical == "missing_database" else [(historical,)])
        assert (await manager.repository.get(profile.id)).credential_ref == reference
        assert (
            await manager.repository.get_secret_transfer(transfer.transfer_id)
        ).state == "adopted"
    finally:
        await original.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
async def test_reauth_binding_rolls_back_with_pointer_when_profile_update_is_rejected(
    tmp_path, backend
):
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from tests.integration.test_connection_manager import _build_manager, _setup

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
    async with connection_databases(tmp_path, backend) as (database, observer):
        manager.repository = ConnectionRepository(database, scope=scope)
        profile = await manager.create(_setup(), credential_value="old", activate=False)
        before = tuple(
            observer.connection.execute(
                "SELECT credential_ref, credential_transfer_id FROM connections WHERE id = ?",
                (str(profile.id),),
            ).fetchone()
        )
        observer.connection.commit()
        if backend == "sqlite":
            database.connection.execute("""CREATE TRIGGER fail_binding BEFORE UPDATE ON connections
                BEGIN SELECT RAISE(ABORT, 'fixture rejects update'); END""")
        else:
            database.connection.execute("""CREATE FUNCTION fail_binding() RETURNS trigger
                LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'fixture rejects update'; END $$""")
            database.connection.execute("""CREATE TRIGGER fail_binding BEFORE UPDATE ON connections
                FOR EACH ROW EXECUTE FUNCTION fail_binding()""")
        database.connection.commit()
        with pytest.raises(Exception, match="fixture rejects update"):
            await manager.reauth(profile.id, "new")
        assert (
            tuple(
                observer.connection.execute(
                    "SELECT credential_ref, credential_transfer_id FROM connections WHERE id = ?",
                    (str(profile.id),),
                ).fetchone()
            )
            == before
        )
        observer.connection.commit()
        assert (await manager.repository.get_secret_transfer(UUID(before[1]))).state == "adopted"
        assert store.resolve(profile.credential_ref) == "old"
        assert sorted(
            row[0]
            for row in observer.connection.execute(
                "SELECT state FROM connection_secret_transfers"
            ).fetchall()
        ) == ["adopted", "discarded"]
        observer.connection.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("closure", ["cancel", "expired", "replaced"])
async def test_cancelled_session_cannot_activate_retained_adoption_after_response_loss(
    tmp_path, backend, closure
):
    import asyncio
    import threading

    from domoai.application.connection_manager import ConnectionManagerError
    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.persistence.serialized import SerializedRepositoryProxy, SerializedStorageExecutor
    from tests.integration.test_connection_manager import _build_manager

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    committed, release = threading.Event(), threading.Event()
    async with connection_databases(tmp_path, backend) as (database, observer):
        raw = database.connection

        class DelayedPendingCommit:
            pending = False

            def __getattr__(self, name):
                return getattr(raw, name)

            def execute(self, sql, *args):
                if "SET state = 'adopted'" in sql:
                    self.pending = True
                return raw.execute(sql, *args)

            def commit(self):
                delay, self.pending = self.pending, False
                raw.commit()
                if delay:
                    committed.set()
                    assert release.wait(5)

        attr = "_proxy" if backend == "postgres" else "_connection"
        previous = getattr(database, attr)
        setattr(database, attr, DelayedPendingCommit())
        executor = SerializedStorageExecutor(operation_timeout_seconds=0.2)
        scope = LeaseScope(tenant_id="default", household_id="default", deployment_id="default")
        manager.repository = SerializedRepositoryProxy(
            ConnectionRepository(database, scope=scope), executor
        )
        session, candidates = await manager.start_setup("fixture")
        await manager.select_setup(session, candidates[0].candidate_id)
        setup = await manager.setup_draft(
            session, endpoint={}, options={}, credential_value="owned"
        )
        task = asyncio.create_task(manager.create_setup(session, activate=False))
        try:
            assert await asyncio.to_thread(committed.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            release.set()
            await executor.run(lambda: None)
            identity = observer.connection.execute("SELECT id FROM connections").fetchone()[0]
            observer.connection.commit()
            if closure == "cancel":
                assert await manager.cancel_setup(session)
            elif closure == "expired":
                from datetime import UTC, datetime

                manager.onboarding._sessions[session]["expires_at"] = datetime.now(UTC) - timedelta(
                    seconds=1
                )
            else:
                await manager.setup_draft(
                    session, endpoint={}, options={}, credential_value="other"
                )
            try:
                await manager.create_setup(session)
            except (ValueError, ConnectionManagerError):
                pass
            assert manager.runtime.adapters == ()
            assert observer.connection.execute("SELECT enabled FROM connections").fetchone()[0] == 0
            observer.connection.commit()
            assert (await manager.list())[0].id == UUID(identity)
            assert store.resolve(setup.draft.credential_ref) == "owned"
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await executor.close()
            setattr(database, attr, previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("state", ["pending", "discarding"])
@pytest.mark.parametrize("foreign", [False, True])
async def test_orphan_transfer_recovery_is_scoped_and_never_disposes_files(
    tmp_path, backend, state, foreign
):
    from uuid import uuid4

    from domoai.domain.coordination import LeaseScope
    from domoai.persistence.connection_repository import ConnectionRepository, SecretTransfer
    from tests.fixtures.household_onboarding import restarted_connection_probe
    from tests.integration.test_connection_manager import _build_manager

    manager, prototype_db, _, store = await _build_manager(tmp_path)
    await prototype_db.close()
    scope = LeaseScope(
        tenant_id="default",
        household_id="foreign" if foreign else "default",
        deployment_id="default",
    )
    async with connection_databases(tmp_path, backend) as (database, observer):
        repository = ConnectionRepository(database, scope=scope)
        owner = object()
        reference = store.put_temporary("private-orphan", owner=owner)
        prepared = store.prepare_temporary(reference, owner=owner)
        transfer = SecretTransfer(uuid4(), reference, uuid4(), "fixture", "orphan", prepared.digest)
        await repository.prepare_secret_transfer(transfer)
        store.materialize(prepared)
        if state == "discarding":
            assert await repository.begin_secret_discard(transfer)
        before = {path.name: path.read_bytes() for path in store.secret_dir.glob("*.json")}
        result = await restarted_connection_probe(database, backend, tmp_path)
        assert [item["code"] for item in result["diagnostics"]] == (
            [] if foreign else ["connection_recovery_pending"]
        )
        assert result["profiles"] == [] and result["children"] == 0
        assert result["owned_transfers"] == 0
        assert {path.name: path.read_bytes() for path in store.secret_dir.glob("*.json")} == before
        assert (await repository.get_secret_transfer(transfer.transfer_id)).state == state
