import asyncio

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.models import Command, Plan, PlanStatus


class _ReadbackBarrier(SimulatedHomeAdapter):
    def __init__(self):
        super().__init__()
        self.written = False
        self.reading, self.release = asyncio.Event(), asyncio.Event()
        self.disconnected = False

    async def execute(self, command, context=None):
        ack = await super().execute(command, context)
        self.written = True
        return ack

    async def read_state(self, source_refs):
        if self.written:
            self.reading.set()
            await self.release.wait()
            assert not self.disconnected
        return await super().read_state(source_refs)

    async def disconnect(self):
        self.disconnected = True
        await super().disconnect()


@pytest.mark.composition
@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_runtime_shutdown_drains_readback_before_disconnect(tmp_path, cancel_close):
    adapter = _ReadbackBarrier()
    runtime = await build_runtime(
        Settings(database_path=tmp_path / "runtime.sqlite3"), adapter=adapter
    )
    await runtime.discovery.refresh()
    device = next(d.id for d in runtime.registry.devices if d.type.value == "light")
    plan = runtime.plan_service.validate(
        Plan(
            id="shutdown-plan",
            commands=[
                Command(
                    id="shutdown-command",
                    device_id=device,
                    command="turn_on",
                    idempotency_key="shutdown-intent",
                ),
            ],
        )
    )
    await runtime.plan_repository.save(plan)
    execute = asyncio.create_task(runtime.facade.executor.execute(plan))
    await asyncio.wait_for(adapter.reading.wait(), 2)
    close = asyncio.create_task(runtime.close())
    try:
        await asyncio.sleep(0.02)
        if cancel_close:
            close.cancel()
            await asyncio.sleep(0.02)
        assert not adapter.disconnected
        assert not close.done()
    finally:
        adapter.release.set()
        summary = await execute
        await asyncio.gather(close, return_exceptions=True)
        if cancel_close:
            from domoai.persistence.repositories import PlanRepository
            from domoai.persistence.sqlite import SQLiteDatabase

            database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
            await database.initialize()
            try:
                stored = await PlanRepository(database).get(plan.id)
                assert stored.status is PlanStatus.COMPLETED
            finally:
                await database.close()
            assert close.cancelled()
    assert summary.outcomes[0].status.value == "confirmed_success"
    assert adapter.disconnected
    assert runtime.lifecycle.running_task_count == 0
    await runtime.close()
