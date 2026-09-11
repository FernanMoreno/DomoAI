import asyncio

import pytest


async def _blocking_runner() -> None:
    await asyncio.Event().wait()


async def _failed_runner() -> None:
    raise RuntimeError("secret runner failure")


@pytest.mark.asyncio
async def test_runtime_lifecycle_starts_each_background_runner_once() -> None:
    from domoai.application.runtime_lifecycle import RuntimeLifecycle

    lifecycle = RuntimeLifecycle(
        event_runner=_blocking_runner,
        scheduler_runner=_blocking_runner,
        supervisor_runner=_blocking_runner,
        outbox_runner=_blocking_runner,
    )

    await lifecycle.start()
    await lifecycle.start()
    await asyncio.sleep(0)

    assert lifecycle.running_task_count == 4

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


@pytest.mark.asyncio
async def test_runtime_lifecycle_reports_each_configured_required_task_by_name() -> None:
    from domoai.application.runtime_lifecycle import RuntimeLifecycle

    lifecycle = RuntimeLifecycle(
        event_runner=_blocking_runner,
        scheduler_runner=_failed_runner,
        state_refresh_runner=_blocking_runner,
        outbox_runner=_blocking_runner,
    )

    await lifecycle.start()
    await asyncio.sleep(0)
    lifecycle.scheduler_runner = None

    assert lifecycle.required_task_states == {
        "domoai-event-consumer": "running",
        "domoai-scheduler": "failed",
        "domoai-state-refresher": "running",
        "domoai-audit-outbox-dispatcher": "running",
    }

    await lifecycle.close()


@pytest.mark.asyncio
async def test_runtime_lifecycle_cleans_up_started_tasks_when_later_runner_fails_synchronously(
) -> None:
    from domoai.application.runtime_lifecycle import RuntimeLifecycle

    async def event_runner() -> None:
        await asyncio.Event().wait()

    def scheduler_runner() -> None:
        raise RuntimeError("startup failed")

    lifecycle = RuntimeLifecycle(
        event_runner=event_runner,
        scheduler_runner=scheduler_runner,  # type: ignore[arg-type]
    )

    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            await lifecycle.start()
        await asyncio.sleep(0)
        assert not any(
            task.get_name() == "domoai-event-consumer" for task in asyncio.all_tasks()
        )
        assert lifecycle.running_task_count == 0
        assert not lifecycle.started
    finally:
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and task.get_name() == "domoai-event-consumer":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_lifecycles_keep_required_worker_states_isolated() -> None:
    from domoai.application.runtime_lifecycle import RuntimeLifecycle

    failed_runtime = RuntimeLifecycle(
        event_runner=_blocking_runner,
        scheduler_runner=_failed_runner,
    )
    healthy_runtime = RuntimeLifecycle(
        event_runner=_blocking_runner,
        scheduler_runner=_blocking_runner,
    )

    await failed_runtime.start()
    await healthy_runtime.start()
    await asyncio.sleep(0)

    assert failed_runtime.required_task_states["domoai-scheduler"] == "failed"
    assert healthy_runtime.required_task_states == {
        "domoai-event-consumer": "running",
        "domoai-scheduler": "running",
    }

    await failed_runtime.close()
    await healthy_runtime.close()
