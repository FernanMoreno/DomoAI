from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.models import (
    AdapterExecutionAck,
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    SourceRef,
    StateSnapshot,
    StateStatus,
    TakeoverResult,
)
from domoai.optimizer.energy import BatteryControlPolicy
from domoai.runtime.clock import FixedClock
from domoai.runtime.control_takeover import (
    BatteryControlCoordinator,
    ControlTakeoverRequest,
)
from domoai.runtime.execution_context import ExecutionContext
from domoai.runtime.state_store import StateStore


class FakeControlAdapter:
    def __init__(self, result: TakeoverResult) -> None:
        self.result = result
        self.requests: list[ControlTakeoverRequest] = []

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        self.requests.append(request)
        return self.result.model_copy(
            update={"plan_id": request.plan_id, "first_command_id": request.first_command_id}
        )


class SupervisedControlAdapter(FakeControlAdapter):
    def __init__(
        self,
        result: TakeoverResult,
        *,
        readback_kw: float,
        renewal: TakeoverResult | None = None,
    ) -> None:
        super().__init__(result)
        self.readback_kw = readback_kw
        self.renewal = renewal
        self.commands: list[Command] = []

    async def renew_control(self, result: TakeoverResult) -> TakeoverResult | None:
        return self.renewal

    async def execute(
        self, command: Command, execution_context: ExecutionContext | None = None
    ) -> AdapterExecutionAck:
        self.commands.append(command)
        return AdapterExecutionAck(
            accepted=True,
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
        )

    async def read_state(self, source_refs: object) -> list[StateSnapshot]:
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        return [
            StateSnapshot(
                device_id="battery.power",
                capability="battery.power",
                value=self.readback_kw,
                unit="kW",
                observed_at=now,
                received_at=now,
                status=StateStatus.CURRENT,
                source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
            )
        ]


def _result(*, confirmed: bool = True) -> TakeoverResult:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    return TakeoverResult(
        lease_id="lease-1",
        status=(ControlLeaseStatus.ACQUIRED if confirmed else ControlLeaseStatus.REJECTED),
        owner="domoai",
        device_id="battery.home",
        plan_id="plan-1",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
        baseline=PhysicalBaseline(
            device_id="battery.home",
            capability="battery.power",
            power_kw=2.0,
            observed_at=now,
            received_at=now,
            state_revision="power:4",
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
            native_scheduler_status="active",
        ),
        first_command_id="command-1",
        first_command_confirmed=confirmed,
        confirmed_at=now if confirmed else None,
        failure_code=None if confirmed else "takeover_readback_failed",
        evidence_digest="sha256:evidence",
    )


@pytest.mark.asyncio
async def test_battery_coordinator_requests_control_for_first_command() -> None:
    adapter = FakeControlAdapter(_result())
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(
            owner="domoai",
            native_scheduler_status="active",
            allow_native_takeover=True,
        ),
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )

    result = await coordinator.acquire_for_plan(
        plan_id="plan-1", commands=[command]
    )

    assert result is not None and result.first_command_confirmed
    assert adapter.requests[0].first_command_id == "command-1"
    assert adapter.requests[0].allow_native_takeover is True


@pytest.mark.asyncio
async def test_unknown_native_owner_fails_closed_without_adapter_call() -> None:
    adapter = FakeControlAdapter(_result())
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="unknown"),
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )

    result = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert result is not None
    assert result.status is ControlLeaseStatus.REJECTED
    assert result.failure_code == "native_owner_unknown"
    assert adapter.requests == []


@pytest.mark.asyncio
async def test_unconfirmed_first_readback_is_not_acquired() -> None:
    adapter = FakeControlAdapter(_result(confirmed=False))
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )

    result = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert result is not None
    assert result.status is ControlLeaseStatus.REJECTED
    assert result.first_command_confirmed is False


@pytest.mark.asyncio
async def test_emergency_stop_requires_zero_readback_before_releasing_lease() -> None:
    adapter = SupervisedControlAdapter(_result(), readback_kw=1.5)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        power_feedback_capability="battery.power",
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="charge_battery",
        idempotency_key="command-key",
    )
    await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert (
        await coordinator.emergency_stop(
            plan_id="plan-1", execution_attempt_id="attempt-1"
        )
        is False
    )
    result = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert result is not None
    assert result.status is not ControlLeaseStatus.RELEASED
    assert result.failure_code == "emergency_stop_readback_failed"
    assert [item.command for item in adapter.commands] == ["stop_battery"]


@pytest.mark.asyncio
async def test_failed_renewal_stops_and_blocks_new_battery_orders() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = SupervisedControlAdapter(_result(), readback_kw=0.0)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(
            owner="domoai",
            native_scheduler_status="disabled",
            lease_seconds=300,
        ),
        clock=clock,
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="charge_battery",
        idempotency_key="command-key",
    )
    await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])
    clock.set(now + timedelta(minutes=4, seconds=30))

    assert await coordinator.supervise_once() == ["plan-1"]
    blocked = await coordinator.acquire_for_plan(plan_id="plan-2", commands=[command])

    assert blocked is not None
    assert blocked.status is ControlLeaseStatus.REJECTED
    assert blocked.failure_code == "control_authority_blocked"
    assert [item.command for item in adapter.commands] == ["stop_battery"]


@pytest.mark.asyncio
async def test_startup_reconciliation_blocks_control_when_zero_readback_is_not_confirmed() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    state_store = StateStore(clock=clock)
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=2.0,
            unit="kW",
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
        )
    )
    adapter = SupervisedControlAdapter(_result(), readback_kw=2.0)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        clock=clock,
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="charge_battery",
        idempotency_key="command-key",
    )

    assert await coordinator.reconcile_startup() is False
    blocked = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert blocked is not None
    assert blocked.status is ControlLeaseStatus.REJECTED
    assert blocked.failure_code == "control_authority_blocked"
    assert [item.command for item in adapter.commands] == ["stop_battery"]


@pytest.mark.asyncio
async def test_startup_reconciliation_persists_confirmed_zero_readback() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    state_store = StateStore(clock=clock)
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=2.0,
            unit="kW",
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
        )
    )
    coordinator = BatteryControlCoordinator(
        SupervisedControlAdapter(_result(), readback_kw=0.0),
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True
    snapshot = state_store.peek("battery.home", "battery.power")
    assert snapshot is not None and snapshot.value == 0.0
