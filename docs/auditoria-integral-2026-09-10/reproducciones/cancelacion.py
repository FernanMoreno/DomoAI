import asyncio
import tempfile
from pathlib import Path

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.household_queue import HouseholdWorkQueues
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.models import Command, Plan
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


async def main():
    with tempfile.TemporaryDirectory(prefix="domoai-cancel-") as d:
        db = SQLiteDatabase(Path(d) / "proof.sqlite")
        await db.initialize()
        adapter = SimulatedHomeAdapter()
        registry = DeviceRegistry()
        states = StateStore()
        audit = AuditLog()
        await DiscoveryService(adapter, registry, states, audit).refresh()
        service = PlanService(registry, states, PolicyEngine([]), audit)
        repo = PlanRepository(db)
        queues = HouseholdWorkQueues()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def block():
            entered.set()
            await release.wait()

        blocker = asyncio.create_task(queues.submit("default", block))
        await entered.wait()
        executor = PlanExecutor(
            adapter, service, audit, plan_repository=repo, household_work_queues=queues
        )
        light = next(device.id for device in registry.devices if device.type.value == "light")
        plan = service.validate(
            Plan(
                id="cancelled",
                commands=[
                    Command(id="c", device_id=light, command="turn_on", idempotency_key="key")
                ],
            )
        )
        await repo.save(plan)
        task = asyncio.create_task(executor.execute(plan))
        for _ in range(100):
            if queues.depths().get("default") == 2:
                break
            await asyncio.sleep(0.001)
        assert queues.depths().get("default") == 2
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        print(
            "plan_after_cancel=",
            (await repo.get(plan.id)).status.value,
            "calls=",
            len(adapter.calls),
        )
        release.set()
        await blocker
        await queues.close()
        print(
            "plan_after_queue_drain=",
            (await repo.get(plan.id)).status.value,
            "calls=",
            len(adapter.calls),
        )
        await db.close()


asyncio.run(main())
