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
    ControlTakeoverGroup,
    ControlTakeoverRequest,
    EVControlCoordinator,
)
from domoai.runtime.state_store import StateStore


class FakeControlAdapter:
    def __init__(self, result: TakeoverResult) -> None:
        self.result = result
        self.requests: list[ControlTakeoverRequest] = []
        self.executed: list[Command] = []

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        self.requests.append(request)
        return self.result

    async def execute(self, command: Command, execution_context=None) -> AdapterExecutionAck:
        self.executed.append(command)
        return AdapterExecutionAck(accepted=True)


class ReadbackControlAdapter(FakeControlAdapter):
    def __init__(self, result: TakeoverResult, readback_kw: float) -> None:
        super().__init__(result)
        self.readback_kw = readback_kw

    async def read_state(self, source_refs):
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        return [
            StateSnapshot(
                device_id="battery.home",
                capability="battery.power",
                value=self.readback_kw,
                observed_at=now,
                received_at=now,
                status=StateStatus.CURRENT,
                source_ref=source_refs[0],
            )
        ]


class EVReadbackAdapter:
    def __init__(self, readback_kw: float = 0.0, *, stop_works: bool = True) -> None:
        self.readback_kw = readback_kw
        self.stop_works = stop_works
        self.executed: list[Command] = []

    async def execute(self, command: Command, execution_context=None) -> AdapterExecutionAck:
        self.executed.append(command)
        if command.command == "stop_ev" and self.stop_works:
            self.readback_kw = 0.0
        return AdapterExecutionAck(accepted=True)

    async def read_state(self, source_refs):
        now = datetime(2026, 8, 23, 12, tzinfo=UTC)
        return [
            StateSnapshot(
                device_id="ev.home",
                capability="ev_charging",
                value=self.readback_kw,
                unit="kW",
                observed_at=now,
                received_at=now,
                status=StateStatus.CURRENT,
                source_ref=source_refs[0],
            )
        ]


def _ev_command(command: str = "charge_ev", value: float = 3.0) -> Command:
    return Command(
        id=f"ev-{command}",
        device_id="ev.home",
        command=command,
        value=value,
        unit="kW",
        idempotency_key=f"ev-{command}-key",
    )


@pytest.mark.asyncio
async def test_startup_reconciliation_is_required_before_acquiring_control() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = ReadbackControlAdapter(_result(), readback_kw=0.5)
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="fixture", external_id="battery.power")
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=1.0,
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
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

    blocked_before_reconciliation = await coordinator.acquire_for_plan(
        plan_id="plan-1", commands=[command]
    )

    assert blocked_before_reconciliation is not None
    assert blocked_before_reconciliation.failure_code == "startup_reconciliation_required"
    assert adapter.requests == []
    assert await coordinator.reconcile_startup() is False
    assert await coordinator.assert_still_owned(plan_id="plan-1") is False

    adapter.readback_kw = 0.0
    assert await coordinator.reconcile_startup() is True
    acquired = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    assert acquired is not None
    assert acquired.status is ControlLeaseStatus.ACQUIRED
    assert len(adapter.requests) == 1


@pytest.mark.asyncio
async def test_ev_control_requires_live_readback_and_stops_on_shutdown() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = EVReadbackAdapter()
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="home_assistant", external_id="number.ev_power")
    await state_store.save(
        StateSnapshot(
            device_id="ev.home",
            capability="ev_charging",
            value=0.0,
            unit="kW",
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
    coordinator = EVControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        device_id="ev.home",
        command_names=frozenset({"charge_ev", "stop_ev"}),
        stop_command="stop_ev",
        stop_unit="kW",
        state_store=state_store,
        power_feedback_capability="ev_charging",
        power_feedback_source_ref=source_ref,
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True
    result = await coordinator.acquire_for_plan(
        plan_id="ev-plan", commands=[_ev_command()]
    )

    assert result is not None
    assert result.status is ControlLeaseStatus.ACQUIRED
    assert await coordinator.assert_still_owned(plan_id="ev-plan") is True

    await coordinator.shutdown()

    assert [command.command for command in adapter.executed] == ["stop_ev"]


@pytest.mark.asyncio
async def test_ev_readback_accepts_enriched_registry_source_metadata() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = EVReadbackAdapter()
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(
        adapter_id="home_assistant",
        external_id="number.ev_power",
        external_type="sensor",
        source_device_id="ha-ev-1",
    )
    await state_store.save(
        StateSnapshot(
            device_id="ev.home",
            capability="ev_charging",
            value=0.0,
            unit="kW",
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(
                adapter_id="home_assistant", external_id="number.ev_power"
            ),
        )
    )
    coordinator = EVControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        device_id="ev.home",
        command_names=frozenset({"charge_ev", "stop_ev"}),
        stop_command="stop_ev",
        stop_unit="kW",
        state_store=state_store,
        power_feedback_capability="ev_charging",
        power_feedback_source_ref=source_ref,
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True


@pytest.mark.asyncio
async def test_feedback_freshness_uses_receipt_time_not_source_observation_time() -> None:
    observed_at = datetime(2026, 8, 22, 12, tzinfo=UTC)
    received_at = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(received_at)
    adapter = EVReadbackAdapter()
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="fixture", external_id="ev.power")
    await state_store.save(
        StateSnapshot(
            device_id="ev.home",
            capability="ev_charging",
            value=0.0,
            unit="kW",
            observed_at=observed_at,
            received_at=received_at,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
    coordinator = EVControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        device_id="ev.home",
        command_names=frozenset({"charge_ev", "stop_ev"}),
        stop_command="stop_ev",
        stop_unit="kW",
        state_store=state_store,
        power_feedback_capability="ev_charging",
        power_feedback_source_ref=source_ref,
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True


@pytest.mark.asyncio
async def test_ev_startup_reconciliation_blocks_when_stop_readback_is_not_zero() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = EVReadbackAdapter(readback_kw=2.0, stop_works=False)
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="home_assistant", external_id="number.ev_power")
    coordinator = EVControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        device_id="ev.home",
        command_names=frozenset({"charge_ev", "stop_ev"}),
        stop_command="stop_ev",
        stop_unit="kW",
        state_store=state_store,
        power_feedback_capability="ev_charging",
        power_feedback_source_ref=source_ref,
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is False
    result = await coordinator.acquire_for_plan(
        plan_id="ev-plan", commands=[_ev_command()]
    )

    assert result is not None
    assert result.failure_code == "startup_reconciliation_required"
    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_startup_reconciliation"
    ]


class _StubCoordinator:
    def __init__(self, result: TakeoverResult | None) -> None:
        self.result = result
        self.acquired = False
        self.released = False
        self.stopped = False

    async def acquire_for_plan(self, *, plan_id: str, commands: list[Command]):
        if self.result is None:
            return None
        self.acquired = self.result.status is ControlLeaseStatus.ACQUIRED
        return self.result

    async def assert_still_owned(self, *, plan_id: str) -> bool:
        return self.acquired and not self.stopped

    async def emergency_stop(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        self.stopped = True
        return True

    async def release_for_plan(self, *, plan_id: str, execution_attempt_id: str) -> bool:
        self.released = True
        return True

    async def supervise_once(self) -> list[str]:
        return []

    async def shutdown(self) -> None:
        return None


@pytest.mark.asyncio
async def test_control_takeover_group_requires_and_releases_every_active_lease() -> None:
    battery = _StubCoordinator(
        _result().model_copy(update={"plan_id": "mixed-plan", "first_command_id": "battery"})
    )
    ev = _StubCoordinator(
        _result().model_copy(update={"plan_id": "mixed-plan", "first_command_id": "ev"})
    )
    group = ControlTakeoverGroup((battery, ev))

    result = await group.acquire_for_plan(
        plan_id="mixed-plan", commands=[_ev_command()]
    )

    assert result is not None
    assert battery.acquired is True
    assert ev.acquired is True
    assert await group.assert_still_owned(plan_id="mixed-plan") is True
    assert await group.release_for_plan(
        plan_id="mixed-plan", execution_attempt_id="attempt"
    ) is True
    assert battery.released is True
    assert ev.released is True


@pytest.mark.asyncio
async def test_startup_zero_cache_is_rechecked_against_live_readback() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = ReadbackControlAdapter(_result(), readback_kw=0.5)
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="fixture", external_id="battery.power")
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=0.0,
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is False
    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_startup_reconciliation"
    ]
    assert coordinator.startup_reconciled is False


@pytest.mark.asyncio
async def test_startup_reconciliation_reads_configured_route_without_cached_snapshot() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = ReadbackControlAdapter(_result(), readback_kw=0.0)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=StateStore(clock=clock),
        power_feedback_capability="battery.power",
        power_feedback_source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True
    assert adapter.executed == []
    assert coordinator.startup_reconciled is True


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
async def test_one_battery_device_cannot_have_two_active_plan_leases() -> None:
    clock = FixedClock(datetime(2026, 8, 23, 12, tzinfo=UTC))
    adapter = FakeControlAdapter(_result())
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        clock=clock,
    )
    first = Command(
        id="command-1",
        device_id="battery.home",
        command="charge_battery",
        idempotency_key="command-key-1",
    )
    second = first.model_copy(
        update={"id": "command-2", "idempotency_key": "command-key-2"}
    )

    acquired = await coordinator.acquire_for_plan(plan_id="plan-1", commands=[first])
    blocked = await coordinator.acquire_for_plan(plan_id="plan-2", commands=[second])

    assert acquired is not None and acquired.status is ControlLeaseStatus.ACQUIRED
    assert blocked is not None
    assert blocked.failure_code == "control_lease_already_held"
    assert [request.plan_id for request in adapter.requests] == ["plan-1"]


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
async def test_supervisor_stops_before_unrenewable_lease_expires() -> None:
    initial = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    adapter = FakeControlAdapter(_result())
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        clock=clock,
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )
    await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])

    clock.set(initial + timedelta(minutes=4, seconds=30))
    stopped = await coordinator.supervise_once()

    assert stopped == ["plan-1"]
    assert [command.command for command in adapter.executed] == ["stop_battery"]
    assert await coordinator.assert_still_owned(plan_id="plan-1") is False


@pytest.mark.asyncio
async def test_emergency_stop_requires_zero_power_readback_when_configured() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = ReadbackControlAdapter(_result(), readback_kw=0.5)
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="fixture", external_id="battery.power")
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=0.0,
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        power_feedback_tolerance_kw=0.05,
        clock=clock,
    )
    command = Command(
        id="command-1",
        device_id="battery.home",
        command="stop_battery",
        idempotency_key="command-key",
    )
    adapter.readback_kw = 0.0
    assert await coordinator.reconcile_startup() is True
    await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])
    adapter.readback_kw = 0.5

    assert (
        await coordinator.emergency_stop(plan_id="plan-1", execution_attempt_id="attempt-1")
        is False
    )
    assert await coordinator.assert_still_owned(plan_id="plan-1") is False


@pytest.mark.asyncio
async def test_release_for_plan_stops_and_confirms_zero_feedback() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = ReadbackControlAdapter(_result(), readback_kw=0.0)
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="fixture", external_id="battery.power")
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=0.0,
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
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

    assert await coordinator.reconcile_startup() is True
    assert await coordinator.acquire_for_plan(plan_id="plan-1", commands=[command])
    assert (
        await coordinator.release_for_plan(
            plan_id="plan-1", execution_attempt_id="release-attempt"
        )
        is True
    )

    assert [item.intent for item in adapter.executed] == [
        "control_supervisor_emergency_stop"
    ]
    assert await coordinator.assert_still_owned(plan_id="plan-1") is False


@pytest.mark.asyncio
async def test_startup_reconciliation_stops_nonzero_latched_feedback() -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = ReadbackControlAdapter(_result(), readback_kw=0.0)
    state_store = StateStore(clock=clock)
    source_ref = SourceRef(adapter_id="fixture", external_id="battery.power")
    await state_store.save(
        StateSnapshot(
            device_id="battery.home",
            capability="battery.power",
            value=1.0,
            observed_at=now,
            received_at=now,
            status=StateStatus.CURRENT,
            source_ref=source_ref,
        )
    )
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        power_feedback_tolerance_kw=0.05,
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True
    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_startup_reconciliation"
    ]
