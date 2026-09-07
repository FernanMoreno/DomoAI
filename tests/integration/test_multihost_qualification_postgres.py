from __future__ import annotations

import pytest

from domoai.application.multihost_qualification import PsycopgPostgresHaProbe


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _docker_available(), reason="Docker is unavailable")
@pytest.mark.asyncio
async def test_real_single_postgres_cannot_be_mistaken_for_synchronous_ha() -> None:
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        "postgres:16-alpine",
        username="domoai",
        password="domoai-test",
        dbname="domoai",
        driver=None,
    ) as container:
        observation = await PsycopgPostgresHaProbe(
            container.get_connection_url(driver=None)
        ).inspect()

    assert observation.is_primary is True
    assert observation.synchronous_replicas == 0
