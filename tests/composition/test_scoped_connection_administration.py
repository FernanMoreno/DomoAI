from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.coordination import LeaseScope
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from domoai.security.secrets import FileSecretStore, SecretUnavailableError

pytestmark = pytest.mark.composition


@pytest.mark.asyncio
async def test_runtime_binds_pending_credentials_to_server_scope(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "runtime.db",
        connection_master_key_path=tmp_path / "master.key",
        mcp_tenant_id="tenant",
        mcp_household_id="home-a",
        mcp_deployment_id="edge",
        home_assistant_token=SecretStr("server-token"),
    )
    runtime = await build_runtime(settings, adapter=ManagedAdapterSet(), control_plane_mode=True)
    try:
        assert runtime.connection_manager is not None
        store = runtime.connection_manager.secret_store
        assert store is not None
        assert store.resolve("env:DOMOAI_HOME_ASSISTANT_TOKEN") == "server-token"
        reference = store.put("pending")
        other = FileSecretStore(
            settings.connection_master_key_path,
            scope=LeaseScope(
                tenant_id="tenant",
                household_id="home-b",
                deployment_id="edge",
            ),
        )
        with pytest.raises(SecretUnavailableError):
            other.resolve(reference)
    finally:
        await runtime.close()


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_postgres_competing_adoptions_publish_one_immutable_household() -> None:
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from testcontainers.community.postgres import PostgresContainer

    from domoai.admin.connection_adoption import (
        AdoptionEntry,
        AdoptionManifest,
        ConnectionAdoptionError,
        adopt_connections,
        profile_digest,
    )
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.persistence.postgres import PostgresDatabase
    from tests.unit.persistence.test_connection_repository import _profile

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        dsn = container.get_connection_url(driver=None)
        database = PostgresDatabase(dsn)
        await database.initialize()
        scope_a = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
        scope_b = scope_a.model_copy(update={"household_id": "home-b"})
        profile = _profile().model_copy(update={"credential_ref": None})
        await ConnectionRepository(database, scope=scope_a).save(profile)
        database.connection.execute("DELETE FROM connection_ownership")
        database.connection.commit()
        barrier = threading.Barrier(2)

        def adopt(scope: LeaseScope) -> str:
            contender = PostgresDatabase(dsn)
            asyncio.run(contender.open_existing())
            manifest = AdoptionManifest(
                scope=scope,
                entries=(
                    AdoptionEntry(
                        connection_id=profile.id,
                        expected_profile_digest=profile_digest(profile),
                    ),
                ),
            )
            try:
                barrier.wait(timeout=5)
                adopt_connections(contender, manifest, apply=True)
                return "adopted"
            except ConnectionAdoptionError:
                return "conflict"
            finally:
                asyncio.run(contender.close())

        try:
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(adopt, scope) for scope in (scope_a, scope_b)]
                assert sorted(future.result(timeout=15) for future in futures) == [
                    "adopted",
                    "conflict",
                ]
            owner = database.connection.execute(
                "SELECT household_id FROM connection_ownership WHERE connection_id = ?",
                (str(profile.id),),
            ).fetchone()[0]
            foreign = ConnectionRepository(
                database, scope=scope_b if owner == "home-a" else scope_a
            )
            assert await foreign.get(profile.id) is None
            assert await foreign.list_all() == []
            assert not await foreign.set_enabled(profile.id, True)
            assert not await foreign.set_credential_ref(profile.id, "secret:foreign")
            assert not await foreign.delete(profile.id)
            assert (
                database.connection.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 1
            )
        finally:
            await database.close()


@pytest.mark.asyncio
async def test_foreign_uuid_has_no_manager_adapter_or_secret_side_effects(tmp_path: Path) -> None:
    import httpx

    from domoai.application.authority import AuthorityPolicy
    from domoai.application.connection_manager import ConnectionManager, ConnectionManagerError
    from domoai.application.connection_providers import (
        ConnectionProviderManifest,
        ConnectionProviderRegistry,
        FixtureConnectionProvider,
    )
    from domoai.management.api import create_management_app
    from domoai.persistence.connection_repository import ConnectionRepository
    from domoai.persistence.sqlite import SQLiteDatabase
    from tests.integration.test_connection_manager import FakeAdapter, _setup

    database = SQLiteDatabase(tmp_path / "shared.db")
    await database.initialize()
    scope = LeaseScope(tenant_id="tenant", household_id="home-a", deployment_id="edge")
    scope_b = scope.model_copy(update={"household_id": "home-b"})
    own_store = FileSecretStore(tmp_path / "master.key", scope=scope)
    foreign_store = FileSecretStore(tmp_path / "master.key", scope=scope_b)
    created = []

    def adapter_factory():
        adapter = FakeAdapter()
        created.append(adapter)
        return adapter

    providers = ConnectionProviderRegistry()
    providers.register(
        FixtureConnectionProvider(
            ConnectionProviderManifest(
                connector_id="fixture",
                display_name="Fixture",
                protocol="fixture",
                version="1.0",
            ),
            adapter_factory=adapter_factory,
        )
    )
    own_runtime, foreign_runtime = ManagedAdapterSet(), ManagedAdapterSet()
    owner = ConnectionManager(
        repository=ConnectionRepository(database, scope=scope),
        providers=providers,
        runtime=own_runtime,
        secret_store=own_store,
    )
    foreign = ConnectionManager(
        repository=ConnectionRepository(database, scope=scope_b),
        providers=providers,
        runtime=foreign_runtime,
        secret_store=foreign_store,
    )
    try:
        profile = await owner.create(_setup(), credential_value="owned-secret")
        for operation in (foreign.activate, foreign.disable, foreign.repair, foreign.diagnose):
            with pytest.raises(ConnectionManagerError, match="not found"):
                await operation(profile.id)
        with pytest.raises(ConnectionManagerError, match="not found"):
            await foreign.reauth(profile.id, "foreign-replacement")
        assert await foreign.delete(profile.id) is False
        app = create_management_app(foreign, authority_policy=AuthorityPolicy("tenant", "home-b"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client:
            response = await client.post(f"/api/v1/connections/{profile.id}/disable")
        assert response.status_code == 404
        assert await foreign.list() == []
        assert len(created) == 1 and created[0].connected
        assert own_store.resolve(profile.credential_ref) == "owned-secret"
        assert await owner.get(profile.id) == profile
    finally:
        await own_runtime.disconnect()
        await foreign_runtime.disconnect()
        await database.close()
