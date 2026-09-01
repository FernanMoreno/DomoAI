from pathlib import Path

import pytest

from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_gateway_ownership_is_singleton_and_released_cleanly(tmp_path: Path) -> None:
    from domoai.persistence.repositories import (
        RuntimeOwnershipConflict,
        RuntimeOwnershipRepository,
    )

    database = SQLiteDatabase(tmp_path / "gateway.sqlite3")
    await database.initialize()
    repository = RuntimeOwnershipRepository(database)

    await repository.acquire(
        deployment_id="home-main",
        owner_id="owner-a",
        config_digest="sha256:config",
    )

    with pytest.raises(RuntimeOwnershipConflict):
        await repository.acquire(
            deployment_id="home-main",
            owner_id="owner-b",
            config_digest="sha256:config",
        )

    await repository.release(deployment_id="home-main", owner_id="owner-a")
    await repository.acquire(
        deployment_id="home-main",
        owner_id="owner-b",
        config_digest="sha256:config",
    )

    await database.close()


@pytest.mark.asyncio
async def test_uncertain_gateway_owner_blocks_startup_until_explicit_release(
    tmp_path: Path,
) -> None:
    from domoai.persistence.repositories import (
        RuntimeOwnershipConflict,
        RuntimeOwnershipRepository,
    )

    database = SQLiteDatabase(tmp_path / "gateway.sqlite3")
    await database.initialize()
    repository = RuntimeOwnershipRepository(database)
    await repository.acquire(
        deployment_id="home-main",
        owner_id="owner-a",
        config_digest="sha256:config",
        uncertain=True,
    )

    with pytest.raises(RuntimeOwnershipConflict, match="uncertain"):
        await repository.acquire(
            deployment_id="home-main",
            owner_id="owner-b",
            config_digest="sha256:config",
        )

    await database.close()
