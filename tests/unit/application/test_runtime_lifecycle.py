import asyncio

import pytest


async def _blocking_runner() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_runtime_lifecycle_starts_each_background_runner_once() -> None:
    from domoai.application.runtime_lifecycle import RuntimeLifecycle

    lifecycle = RuntimeLifecycle(
        event_runner=_blocking_runner,
        scheduler_runner=_blocking_runner,
        supervisor_runner=_blocking_runner,
    )

    await lifecycle.start()
    await lifecycle.start()
    await asyncio.sleep(0)

    assert lifecycle.running_task_count == 3

    await lifecycle.close()
    await lifecycle.close()
    assert lifecycle.running_task_count == 0


@pytest.mark.asyncio
async def test_runtime_lifecycle_omits_unconfigured_supervisor() -> None:
    from domoai.application.runtime_lifecycle import RuntimeLifecycle

    lifecycle = RuntimeLifecycle(
        event_runner=_blocking_runner,
        scheduler_runner=_blocking_runner,
    )

    await lifecycle.start()
    await asyncio.sleep(0)

    assert lifecycle.running_task_count == 2
    await lifecycle.close()
