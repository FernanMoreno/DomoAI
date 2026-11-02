import asyncio

import pytest


class _CloseRecorder:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    async def close(self) -> None:
        self.events.append(self.name)


class _Lifecycle:
    started = True
    closed = False
    required_task_states = {
        "domoai-event-consumer": "running",
        "domoai-scheduler": "running",
        "domoai-control-supervisor": "running",
    }

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events

    async def close(self) -> None:
        if self.events is not None:
            self.events.append("lifecycle")
        return None


class _Adapter:
    async def disconnect(self) -> None:
        return None


class _Ownership:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def release(self) -> bool:
        self.events.append("ownership")
        return True


class _Executor:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release

    async def close(self) -> None:
        self.entered.set()
        await self.release.wait()


class _Facade:
    def __init__(self, executor: _Executor) -> None:
        self.executor = executor


@pytest.mark.asyncio
async def test_runtime_releases_physical_ownership_before_storage_shutdown() -> None:
    from domoai.application.runtime_factory import RuntimeComposition

    events: list[str] = []
    runtime = RuntimeComposition.__new__(RuntimeComposition)
    runtime.lifecycle = _Lifecycle()
    runtime.facade = _Facade(_Executor(asyncio.Event(), asyncio.Event()))
    runtime.battery_control_coordinator = None
    runtime.adapter = _Adapter()
    runtime.blocking_workers = []
    runtime.energy_closers = ()
    runtime.ownership = _Ownership(events)
    runtime.storage = _CloseRecorder("storage", events)
    runtime.audit_storage = _CloseRecorder("audit_storage", events)
    runtime.database = _CloseRecorder("database", events)
    runtime.approval_database = _CloseRecorder("approval_database", events)
    runtime.audit_database = _CloseRecorder("audit_database", events)
    runtime.control_supervisor = None
    runtime.mqtt_discovery = None
    runtime.lease_coordinator = None
    runtime.coordination_token = None
    runtime.household_work_queues = None

    close_task = asyncio.create_task(runtime.close())
    await runtime.facade.executor.entered.wait()
    runtime.facade.executor.release.set()
    await close_task

    assert events.index("ownership") < events.index("storage")
    assert events.index("ownership") < events.index("audit_storage")


@pytest.mark.asyncio
async def test_runtime_marks_closing_before_executor_drain_and_keeps_workers_alive(
    tmp_path,
) -> None:
    from domoai.application.runtime_factory import build_runtime
    from domoai.config.settings import Settings
    from domoai.mcp.health import readyz

    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = await build_runtime(
        Settings(
            database_path=tmp_path / "runtime.sqlite3",
            audit_database_path=tmp_path / "audit.sqlite3",
        )
    )
    await runtime.start()
    original_close = runtime.facade.executor.close

    async def drain_barrier() -> None:
        entered.set()
        await release.wait()
        await original_close()

    runtime.facade.executor.close = drain_barrier  # type: ignore[method-assign]
    close_task = asyncio.create_task(runtime.close())
    try:
        await entered.wait()

        response = await readyz(runtime, object())  # type: ignore[arg-type]

        assert runtime.closing
        assert response.status_code == 503
        assert b'"runtime_closing"' in response.body
        assert runtime.lifecycle.required_task_states
        assert all(
            state == "running" for state in runtime.lifecycle.required_task_states.values()
        )

        release.set()
        await close_task
        assert runtime.lifecycle.closed
    finally:
        release.set()
        if not close_task.done():
            await close_task
