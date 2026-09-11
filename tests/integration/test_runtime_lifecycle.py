import asyncio
import json
from pathlib import Path

import pytest

from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.mcp.health import readyz


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

        assert runtime.lifecycle.running_task_count == 4
    finally:
        await runtime.close()
        await runtime.close()

    assert runtime.lifecycle.running_task_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_name",
    [
        "domoai-event-consumer",
        "domoai-scheduler",
        "domoai-state-refresher",
        "domoai-audit-outbox-dispatcher",
    ],
)
async def test_runtime_readiness_rejects_a_failed_configured_worker(
    tmp_path: Path,
    task_name: str,
) -> None:
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            audit_database_path=tmp_path / "runtime-audit.sqlite3",
        )
    )
    try:
        await runtime.start()
        task = runtime.lifecycle.task(task_name)
        assert task is not None
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        response = await readyz(runtime, object())  # type: ignore[arg-type]
        payload = json.loads(response.body)

        assert response.status_code == 503
        assert payload["runtime"]["required_tasks"][task_name] == "cancelled"
        assert f"runtime_task_cancelled:{task_name}" in payload["reason_codes"]
    finally:
        await runtime.close()
