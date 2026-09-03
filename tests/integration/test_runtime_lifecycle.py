from pathlib import Path

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings


@pytest.mark.asyncio
async def test_configured_runtime_has_one_shared_lifecycle_owner(tmp_path: Path) -> None:
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            audit_database_path=tmp_path / "runtime-audit.sqlite3",
        )
    )

    try:
        assert runtime.lifecycle.running_task_count == 0

        await runtime.start()
        await runtime.start()

        assert runtime.lifecycle.running_task_count == 3
    finally:
        await runtime.close()
        await runtime.close()

    assert runtime.lifecycle.running_task_count == 0
