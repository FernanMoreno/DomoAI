from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
from domoai.persistence.coordination import PhysicalIntentRepository
from domoai.persistence.postgres import PostgresDatabase

pytestmark = pytest.mark.composition


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_postgres_control_plane_preserves_idempotent_physical_intents() -> None:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        database = PostgresDatabase(container.get_connection_url(driver=None))
        await database.initialize()
        try:
            repository = PhysicalIntentRepository(database)  # type: ignore[arg-type]
            now = datetime.now(UTC)
            intent = PhysicalIntent(
                tenant_id="tenant",
                household_id="home",
                deployment_id="edge",
                idempotency_key="same-command",
                plan_id="plan-1",
                command_id="command-1",
                fencing_epoch=7,
                created_at=now,
                updated_at=now,
            )
            first = await repository.claim(intent)
            replay = await repository.claim(intent.model_copy(update={"plan_id": "plan-replay"}))

            assert first.status is PhysicalIntentStatus.PREPARED
            assert replay.plan_id == "plan-1"
            assert await repository.count() == 1
        finally:
            await database.close()


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_multi_host_runtime_can_use_postgres_with_injected_test_coordinator() -> None:
    from testcontainers.community.postgres import PostgresContainer

    class FencedFixtureAdapter(SimulatedHomeAdapter):
        supports_fencing = True

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        runtime = await build_runtime(
            Settings(
                multi_host_enabled=True,
                postgres_dsn=container.get_connection_url(driver=None),
                instance_id="host-a",
            ),
            adapter=FencedFixtureAdapter(),
            lease_coordinator=DeterministicLeaseCoordinator(),
        )
        try:
            assert runtime.database.__class__.__name__ == "PostgresDatabase"
            assert runtime.coordination_token is not None
            assert runtime.fencing_guard is not None
        finally:
            await runtime.close()
