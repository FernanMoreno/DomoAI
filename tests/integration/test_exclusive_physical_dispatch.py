import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.coordination import DeterministicLeaseCoordinator, FencingGuard
from domoai.application.discovery_service import DiscoveryService
from domoai.application.execution_admission import ExecutionAdmission
from domoai.application.executor import PlanExecutor
from domoai.application.household_queue import HouseholdWorkQueues
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.domain.coordination import (
    LeaseScope,
    PhysicalIntent,
    PhysicalIntentStatus,
)
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import Command, Plan, PlanStatus, Policy, PolicyAction
from domoai.persistence.coordination import PhysicalIntentRepository
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


@pytest.mark.asyncio
async def test_executor_does_not_dispatch_conflicting_physical_intent(tmp_path: Path) -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    coordinator = DeterministicLeaseCoordinator()
    token = await coordinator.acquire(
        LeaseScope(tenant_id="default", household_id="default", deployment_id="edge"),
        owner_id="owner-a",
        ttl_seconds=60,
    )
    repository = PhysicalIntentRepository(database)
    now = datetime.now(UTC)
    await repository.claim_for_dispatch(
        PhysicalIntent(
            tenant_id="default",
            household_id="default",
            deployment_id="edge",
            idempotency_key="shared-intent",
            plan_id="plan-original",
            command_id="command-original",
            fencing_epoch=token.epoch,
            owner_id="owner-a",
            execution_attempt_id="attempt-original",
            created_at=now,
            updated_at=now,
        )
    )
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        fencing_guard=FencingGuard(coordinator, token),
        physical_intent_repository=repository,
    )
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = plan_service.validate(
        Plan(
            id="plan-conflicting",
            commands=[
                Command(
                    id="command-conflicting",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="shared-intent",
                )
            ],
        )
    )

    try:
        summary = await executor.execute(plan)
    finally:
        await database.close()

    outcome = summary.outcomes[0]
    assert outcome.error is not None
    assert outcome.error.code == ErrorCode.DUPLICATE_COMMAND
    assert outcome.error.retryable is False
    assert adapter.calls == []


@asynccontextmanager
async def _queued_execution(tmp_path: Path, adapter=None, *, deployment_id: str | None = None):
    adapter = adapter or SimulatedHomeAdapter()
    clock = FixedClock(datetime.now(UTC))
    registry = DeviceRegistry()
    states = StateStore(clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, states, audit).refresh()
    service = PlanService(
        registry, states, PolicyEngine([]), audit, clock=clock, deployment_id=deployment_id
    )
    database = SQLiteDatabase(tmp_path / "cancellation.sqlite3")
    await database.initialize()
    repository = PlanRepository(database, clock=clock)
    queues = HouseholdWorkQueues()
    executor = PlanExecutor(
        adapter,
        service,
        audit,
        plan_repository=repository,
        household_work_queues=queues,
        execution_admission=ExecutionAdmission(clock=clock),
        clock=clock,
    )
    light = next(device.id for device in registry.devices if device.type.value == "light")
    plan = service.validate(
        Plan(
            id="queued-plan",
            commands=[
                Command(
                    id="queued-command",
                    device_id=light,
                    command="turn_on",
                    idempotency_key="queued",
                ),
            ],
        )
    )
    await repository.save(plan)
    try:
        yield executor, plan, repository, queues, adapter, clock
    finally:
        await queues.close()
        await database.close()


@asynccontextmanager
async def _blocked_household(queues):
    entered, release = asyncio.Event(), asyncio.Event()

    async def block():
        entered.set()
        await release.wait()

    blocker = asyncio.create_task(queues.submit("default", block))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        yield release
    finally:
        release.set()
        await blocker


async def _wait_queued(queues):
    async with asyncio.timeout(2):
        while queues.depths().get("default") != 2:
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancelled_pending_execution_never_dispatches_after_queue_drain(tmp_path):
    async with _queued_execution(tmp_path) as (executor, plan, repo, queues, adapter, _):
        async with _blocked_household(queues):
            task = asyncio.create_task(executor.execute(plan))
            await _wait_queued(queues)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await queues.close()
        assert adapter.calls == []
        assert (await repo.get(plan.id)).status is PlanStatus.CANCELLED
        with pytest.raises(DomainError):
            await executor.execute(plan)


class _HeldAcknowledgement(SimulatedHomeAdapter):
    def __init__(self):
        super().__init__()
        self.sent, self.release = asyncio.Event(), asyncio.Event()

    async def execute(self, command, context=None):
        ack = await super().execute(command, context)
        self.sent.set()
        await self.release.wait()
        return ack


@pytest.mark.asyncio
async def test_cancel_after_physical_send_preserves_confirmed_result(tmp_path):
    adapter = _HeldAcknowledgement()
    async with _queued_execution(tmp_path, adapter) as (executor, plan, repo, queues, _, _):
        task = asyncio.create_task(executor.execute(plan))
        await asyncio.wait_for(adapter.sent.wait(), 2)
        task.cancel()
        adapter.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await queues.close()
        stored = await repo.get(plan.id)
        assert stored.status is PlanStatus.COMPLETED
        assert stored.execution.outcomes[0].status.value == "confirmed_success"
        assert len(adapter.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expiry", "policy"])
async def test_queued_execution_rechecks_admission_before_dispatch(tmp_path, change):
    async with _queued_execution(tmp_path) as (executor, plan, _, queues, adapter, clock):
        async with _blocked_household(queues) as release:
            task = asyncio.create_task(executor.execute(plan))
            await _wait_queued(queues)
            if change == "expiry":
                clock.set(plan.expires_at + timedelta(seconds=1))
            else:
                executor.plan_service.policy_engine.policies.append(
                    Policy(id="deny", action=PolicyAction.DENY)
                )
            release.set()
            try:
                await task
            except DomainError as error:
                assert error.code in {ErrorCode.STALE_PLAN, ErrorCode.APPROVAL_REQUIRED}
        assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_replay", [False, True])
async def test_executor_serves_confirmed_replay_without_adapter_dispatch(
    tmp_path: Path, cancel_replay
) -> None:
    class HeldReadback(SimulatedHomeAdapter):
        hold = False
        entered, release = asyncio.Event(), asyncio.Event()

        async def read_state(self, source_refs):
            if self.hold:
                self.entered.set()
                await self.release.wait()
            return await super().read_state(source_refs)

    adapter = HeldReadback()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(
        registry, state_store, PolicyEngine([]), audit, deployment_id="edge"
    )
    database = SQLiteDatabase(tmp_path / "ledger.sqlite3")
    await database.initialize()
    coordinator = DeterministicLeaseCoordinator()
    token = await coordinator.acquire(
        LeaseScope(tenant_id="default", household_id="default", deployment_id="edge"),
        owner_id="owner-a",
        ttl_seconds=60,
    )
    repository = PhysicalIntentRepository(database)
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = plan_service.validate(
        Plan(
            id="plan-replay",
            commands=[
                Command(
                    id="command-replay",
                    device_id=light_id,
                    command="turn_on",
                    idempotency_key="replay-intent",
                )
            ],
        )
    )
    now = datetime.now(UTC)
    claimed = await repository.claim_for_dispatch(
        PhysicalIntent(
            tenant_id="default",
            household_id="default",
            deployment_id="edge",
            idempotency_key="replay-intent",
            plan_id=plan.id,
            command_id="command-replay",
            fencing_epoch=token.epoch,
            owner_id="owner-a",
            execution_attempt_id="attempt-original",
            created_at=now,
            updated_at=now,
        )
    )
    await repository.settle(intent=claimed.intent, status=PhysicalIntentStatus.CONFIRMED)
    plans = PlanRepository(database)
    await plans.save(plan)
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        fencing_guard=FencingGuard(coordinator, token),
        physical_intent_repository=repository,
        plan_repository=plans,
    )

    try:
        if cancel_replay:
            adapter.hold = True
            task = asyncio.create_task(executor.execute(plan))
            await asyncio.wait_for(adapter.entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            adapter.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            stored = await plans.get(plan.id)
            assert stored.status is PlanStatus.UNKNOWN
            summary = stored.execution
        else:
            summary = await executor.execute(plan)
    finally:
        await database.close()

    assert summary.outcomes
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_repeated_cancellation_after_send_stops_later_commands(tmp_path):
    adapter = _HeldAcknowledgement()
    async with _queued_execution(tmp_path, adapter) as (executor, original, repo, queues, _, _):
        plan = executor.plan_service.validate(
            Plan(
                id="multiple",
                commands=[
                    original.commands[0].model_copy(
                        update={"id": "first", "idempotency_key": "first"}
                    ),
                    original.commands[0].model_copy(
                        update={"id": "second", "idempotency_key": "second", "command": "turn_off"}
                    ),
                ],
            )
        )
        await repo.save(plan)
        task = asyncio.create_task(executor.execute(plan))
        await asyncio.wait_for(adapter.sent.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        adapter.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        stored = await repo.get(plan.id)
        assert stored.status is PlanStatus.PARTIALLY_FAILED
        assert [o.status.value for o in stored.execution.outcomes] == [
            "confirmed_success",
            "rejected",
        ]
        assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_cancel_while_dispatch_claim_is_waiting_never_writes(tmp_path):
    async with _queued_execution(tmp_path, deployment_id="edge") as (
        executor, plan, repo, _, adapter, clock
    ):
        coordinator = DeterministicLeaseCoordinator(clock=clock)
        from domoai.domain.coordination import LeaseScope

        token = await coordinator.acquire(
            LeaseScope(tenant_id="default", household_id="default", deployment_id="edge"),
            owner_id="owner",
            ttl_seconds=60,
        )
        executor.fencing_guard = FencingGuard(coordinator, token)
        entered, release = asyncio.Event(), asyncio.Event()

        class HeldClaim(PhysicalIntentRepository):
            async def claim_for_dispatch(self, intent):
                result = await super().claim_for_dispatch(intent)
                entered.set()
                await release.wait()
                return result

        ledger = HeldClaim(repo._repository.database)
        executor.physical_intent_repository = ledger
        task = asyncio.create_task(executor.execute(plan))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter.calls == []
        assert (await repo.get(plan.id)).status is PlanStatus.CANCELLED
        intent = await ledger.get(household_id="default", idempotency_key="queued")
        assert intent.status is PhysicalIntentStatus.REJECTED
