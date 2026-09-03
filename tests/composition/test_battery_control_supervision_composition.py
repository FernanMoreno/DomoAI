"""Composition checks for latched-actuator lease and restart safety."""

from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.recovery import PlanRecoveryService
from domoai.domain.models import (
    AdapterExecutionAck,
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    Plan,
    PlanStatus,
    SourceRef,
    StateSnapshot,
    StateStatus,
    TakeoverResult,
)
from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulator
from domoai.optimizer.energy import BatteryControlPolicy
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.control_takeover import BatteryControlCoordinator, ControlTakeoverRequest
from domoai.runtime.events import AuditLog


class _RecordingTakeoverAdapter:
    def __init__(
        self,
        result: TakeoverResult,
        *,
        readback_kw: float = 0.0,
        renew_enabled: bool = True,
    ) -> None:
        self.result = result
        self.readback_kw = readback_kw
        self.renew_enabled = renew_enabled
        self.renewals = 0
        self.executed: list[Command] = []

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        return self.result

    async def renew_control(self, result: TakeoverResult) -> TakeoverResult:
        if not self.renew_enabled:
            raise RuntimeError("renewal unavailable")
        self.renewals += 1
        return result.model_copy(update={"expires_at": result.expires_at + timedelta(minutes=5)})

    async def execute(self, command: Command, execution_context=None) -> AdapterExecutionAck:
        self.executed.append(command)
        return AdapterExecutionAck(accepted=True)

    async def read_state(self, source_refs):
        now = self.result.acquired_at
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


def _takeover_result(now: datetime) -> TakeoverResult:
    return TakeoverResult(
        lease_id="composition-lease",
        status=ControlLeaseStatus.ACQUIRED,
        owner="domoai",
        device_id="battery.home",
        plan_id="composition-plan",
        acquired_at=now,
        expires_at=now + timedelta(minutes=5),
        baseline=PhysicalBaseline(
            device_id="battery.home",
            capability="battery.power",
            power_kw=2.0,
            observed_at=now,
            received_at=now,
            source_ref=SourceRef(adapter_id="fixture", external_id="battery.power"),
            state_revision="power:1",
            native_scheduler_status="disabled",
        ),
        first_command_id="composition-command",
        first_command_confirmed=True,
        confirmed_at=now,
        evidence_digest="sha256:composition-takeover",
    )


def _command() -> Command:
    return Command(
        id="composition-command",
        device_id="battery.home",
        command="charge_battery",
        value=1,
        unit="kW",
        idempotency_key="composition-command-key",
    )


def _lab_profile() -> BatterySimulationProfile:
    return BatterySimulationProfile(
        provider_id="lab-battery-simulator",
        device_id="lab-battery-1",
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=2.0,
        max_soc_kwh=9.0,
        max_charge_kw=4.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        tick_seconds=1.0,
    )


class _SimulatorTakeoverAdapter:
    """Wraps the deterministic lab simulator as a takeover-capable adapter.

    Every physical write and readback in this shim actually goes through
    ``BatterySimulator.command``/``snapshot``, so a simulator fault (e.g.
    ``set_fault("unavailable")``) fails the same call path a real transport
    outage would fail, instead of being asserted separately from the
    simulator's own state machine.
    """

    def __init__(self, simulator: BatterySimulator, *, clock: FixedClock) -> None:
        self.simulator = simulator
        self.clock = clock
        self.executed: list[Command] = []

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        now = self.clock.now()
        snapshot = self.simulator.snapshot()
        source_ref = SourceRef(adapter_id="lab-simulator", external_id=snapshot.device_id)
        return TakeoverResult(
            lease_id=f"sim-lease-{request.plan_id}",
            status=ControlLeaseStatus.ACQUIRED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now + timedelta(seconds=request.lease_seconds),
            baseline=PhysicalBaseline(
                device_id=request.device_id,
                capability="battery.power",
                power_kw=snapshot.power_kw,
                observed_at=snapshot.observed_at,
                received_at=now,
                source_ref=source_ref,
                state_revision=f"power:{snapshot.revision}",
                native_scheduler_status=request.native_scheduler_status,
            ),
            first_command_id=request.first_command_id,
            first_command_confirmed=True,
            confirmed_at=now,
            evidence_digest="sha256:" + "0" * 64,
        )

    async def renew_control(self, result: TakeoverResult) -> TakeoverResult:
        if not self.simulator.snapshot().available:
            raise ConnectionError("battery simulator is unavailable")
        return result.model_copy(
            update={"expires_at": result.expires_at + timedelta(minutes=5)}
        )

    async def execute(self, command: Command, execution_context=None) -> AdapterExecutionAck:
        self.executed.append(command)
        value = command.value if isinstance(command.value, (int, float)) else None
        try:
            self.simulator.command(
                command.command, value=value, idempotency_key=command.idempotency_key
            )
        except (ConnectionError, ValueError):
            return AdapterExecutionAck(accepted=False)
        return AdapterExecutionAck(accepted=True)

    async def read_state(self, source_refs):
        snapshot = self.simulator.snapshot()
        status = StateStatus.CURRENT if snapshot.available else StateStatus.UNAVAILABLE
        now = self.clock.now()
        return [
            StateSnapshot(
                device_id="battery.home",
                capability="battery.power",
                value=snapshot.power_kw,
                observed_at=snapshot.observed_at,
                received_at=now,
                status=status,
                source_ref=source_refs[0],
            )
        ]


@pytest.mark.composition
@pytest.mark.asyncio
async def test_lease_is_renewed_and_shutdown_stops_the_latched_actuator() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = _RecordingTakeoverAdapter(_takeover_result(now))
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(
            owner="domoai",
            native_scheduler_status="disabled",
            lease_seconds=300,
        ),
        clock=clock,
    )

    await coordinator.acquire_for_plan(plan_id="composition-plan", commands=[_command()])
    clock.set(now + timedelta(minutes=4, seconds=30))

    assert await coordinator.supervise_once() == []
    assert adapter.renewals == 1
    assert adapter.executed == []
    assert await coordinator.assert_still_owned(plan_id="composition-plan") is True

    await coordinator.shutdown()

    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_emergency_stop"
    ]
    assert await coordinator.assert_still_owned(plan_id="composition-plan") is False


@pytest.mark.composition
@pytest.mark.asyncio
async def test_lease_renewal_loss_stops_before_expiry_and_revokes_write_authority() -> None:
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = _RecordingTakeoverAdapter(
        _takeover_result(now),
        renew_enabled=False,
    )
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        clock=clock,
    )

    await coordinator.acquire_for_plan(plan_id="composition-plan", commands=[_command()])
    clock.set(now + timedelta(minutes=4, seconds=30))

    assert await coordinator.supervise_once() == ["composition-plan"]
    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_emergency_stop"
    ]
    assert await coordinator.assert_still_owned(plan_id="composition-plan") is False


@pytest.mark.composition
@pytest.mark.asyncio
async def test_startup_recovery_marks_executing_unknown_without_replaying_write(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "recovery.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    audit = AuditLog()
    adapter = _RecordingTakeoverAdapter(_takeover_result(datetime.now(UTC)))
    await repository.save(
        Plan(
            id="crashed-latched-plan",
            status=PlanStatus.EXECUTING,
            commands=[_command()],
        )
    )

    recovered = await PlanRecoveryService(repository, audit).recover_orphaned_plans()

    persisted = await repository.get("crashed-latched-plan")
    assert recovered == ["crashed-latched-plan"]
    assert persisted is not None and persisted.status is PlanStatus.UNKNOWN
    assert adapter.executed == []
    await database.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_startup_reconciliation_stops_and_confirms_zero_readback() -> None:
    from domoai.runtime.state_store import StateStore

    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
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
    adapter = _RecordingTakeoverAdapter(_takeover_result(now), readback_kw=0.0)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True
    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_startup_reconciliation"
    ]


@pytest.mark.composition
@pytest.mark.asyncio
async def test_shutdown_revokes_lease_when_stop_readback_is_not_confirmed() -> None:
    from domoai.runtime.state_store import StateStore

    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
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
    adapter = _RecordingTakeoverAdapter(_takeover_result(now), readback_kw=0.0)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        state_store=state_store,
        power_feedback_capability="battery.power",
        power_feedback_tolerance_kw=0.05,
        clock=clock,
    )

    assert await coordinator.reconcile_startup() is True
    adapter.readback_kw = 0.5
    await coordinator.acquire_for_plan(plan_id="composition-plan", commands=[_command()])
    await coordinator.shutdown()

    assert await coordinator.assert_still_owned(plan_id="composition-plan") is False


@pytest.mark.composition
@pytest.mark.asyncio
async def test_lease_expiry_against_simulator_fault_fails_closed_not_open() -> None:
    """A total simulator outage at lease expiry must revoke authority.

    The supervisor cannot confirm the emergency stop against a genuinely
    unavailable transport, so it must not report the lease released; it
    fails to EXPIRED and physical write authority is revoked either way.
    """

    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    simulator = BatterySimulator(_lab_profile(), clock=clock)
    adapter = _SimulatorTakeoverAdapter(simulator, clock=clock)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="disabled"),
        clock=clock,
    )

    await coordinator.acquire_for_plan(plan_id="composition-plan", commands=[_command()])
    simulator.set_fault("unavailable")
    clock.set(now + timedelta(minutes=4, seconds=30))

    assert await coordinator.supervise_once() == ["composition-plan"]
    assert [command.intent for command in adapter.executed] == [
        "control_supervisor_emergency_stop"
    ]
    assert await coordinator.assert_still_owned(plan_id="composition-plan") is False


@pytest.mark.composition
@pytest.mark.asyncio
async def test_restart_recovery_does_not_replay_command_the_simulator_already_applied(
    tmp_path,
) -> None:
    """A crash-recovered plan must not resend a command the simulator has state for.

    The simulator itself refuses to re-apply a command whose idempotency key
    it has already seen (defense in depth); this proves the recovery service
    additionally never attempts the resend at all.
    """

    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    clock = FixedClock(now)
    simulator = BatterySimulator(_lab_profile(), clock=clock)
    adapter = _SimulatorTakeoverAdapter(simulator, clock=clock)

    applied_before_crash = simulator.command(
        "charge_battery", value=2.0, idempotency_key="crashed-latched-plan:charge"
    )
    assert applied_before_crash.mode == "charging"

    database = SQLiteDatabase(tmp_path / "recovery.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    audit = AuditLog()
    await repository.save(
        Plan(
            id="crashed-latched-plan",
            status=PlanStatus.EXECUTING,
            commands=[
                Command(
                    id="crashed-command",
                    device_id="battery.home",
                    command="charge_battery",
                    value=2.0,
                    unit="kW",
                    idempotency_key="crashed-latched-plan:charge",
                )
            ],
        )
    )

    recovered = await PlanRecoveryService(repository, audit).recover_orphaned_plans()

    persisted = await repository.get("crashed-latched-plan")
    assert recovered == ["crashed-latched-plan"]
    assert persisted is not None and persisted.status is PlanStatus.UNKNOWN
    assert adapter.executed == []

    replayed = simulator.command(
        "charge_battery", value=2.0, idempotency_key="crashed-latched-plan:charge"
    )
    assert replayed == applied_before_crash
    assert replayed.revision == applied_before_crash.revision

    await database.close()
