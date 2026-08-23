from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import (
    Command,
    ControlLeaseStatus,
    PhysicalBaseline,
    Plan,
    SourceRef,
    TakeoverResult,
)
from domoai.optimizer.cp_sat import _proposal_plan
from domoai.optimizer.energy import BatteryActuator, BatteryControlPolicy
from domoai.optimizer.scenario import OptimizationScenario
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import FixedClock
from domoai.runtime.control_takeover import (
    BatteryControlCoordinator,
    ControlTakeoverRequest,
)
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.energy import energy_context_for, energy_horizon


class TakeoverFixtureAdapter(SimulatedHomeAdapter):
    def __init__(self, *args: object, clock: FixedClock, fail_first_readback: bool = False) -> None:
        super().__init__(*args, clock=clock)
        self.clock = clock
        self.fail_first_readback = fail_first_readback
        self.control_requests: list[ControlTakeoverRequest] = []

    async def acquire_control(self, request: ControlTakeoverRequest) -> TakeoverResult:
        self.control_requests.append(request)
        now = self.clock.now()
        return TakeoverResult(
            lease_id="lease-battery-1",
            status=ControlLeaseStatus.ACQUIRED,
            owner=request.owner,
            device_id=request.device_id,
            plan_id=request.plan_id,
            acquired_at=now,
            expires_at=now.replace(second=now.second + 1),
            baseline=PhysicalBaseline(
                device_id=request.device_id,
                capability="power",
                power_kw=2.0,
                observed_at=now,
                received_at=now,
                source_ref=SourceRef(adapter_id=self.adapter_id, external_id="fixture.power"),
                state_revision="physical:17",
                native_scheduler_status="active",
            ),
            first_command_id=request.first_command_id,
            first_command_confirmed=False,
            evidence_digest="sha256:takeover-fixture",
        )

    async def read_state(self, source_refs: object) -> list[object]:
        if self.fail_first_readback and self.calls:
            self.fail_first_readback = False
            return []
        return await super().read_state(source_refs)  # type: ignore[arg-type,return-value]


def _commands(device_id: str) -> list[Command]:
    return [
        Command(
            id="battery-takeover-stop",
            device_id=device_id,
            command="turn_off",
            idempotency_key="battery-takeover-stop-key",
        ),
        Command(
            id="battery-takeover-next",
            device_id=device_id,
            command="turn_on",
            idempotency_key="battery-takeover-next-key",
        ),
    ]


@pytest.mark.composition
@pytest.mark.asyncio
async def test_takeover_composes_baseline_executor_readback_and_sqlite(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = TakeoverFixtureAdapter(clock=clock)
    adapter._find("light.living_room_main")["state"]["power"] = True
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog(clock=clock)
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")

    database = SQLiteDatabase(tmp_path / "battery-takeover.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    commands = _commands(device_id)
    plan = plan_service.validate(Plan(id="battery-takeover-plan", commands=commands))
    await plan_repository.save_validation(plan)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(
            owner="domoai",
            native_scheduler_status="active",
            allow_native_takeover=True,
        ),
        device_id=device_id,
        command_names=frozenset({"turn_off", "turn_on"}),
        clock=clock,
    )
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        control_takeover=coordinator,
        clock=clock,
    )

    summary = await executor.execute(plan)

    assert [outcome.status.value for outcome in summary.outcomes] == [
        "confirmed_success",
        "confirmed_success",
    ]
    assert [call.command for call in adapter.calls] == ["turn_off", "turn_on"]
    assert adapter.control_requests[0].first_command_id == commands[0].id
    assert any(event.event_type == "control_takeover_result" for event in audit.events)
    persisted = await plan_repository.get(plan.id)
    assert persisted is not None and persisted.status.value == "completed"


@pytest.mark.composition
@pytest.mark.asyncio
async def test_takeover_readback_failure_blocks_later_physical_slots(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = TakeoverFixtureAdapter(clock=clock, fail_first_readback=True)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog(clock=clock)
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    database = SQLiteDatabase(tmp_path / "battery-takeover-failure.sqlite3", clock=clock)
    await database.initialize()
    plan_repository = PlanRepository(database, clock=clock)
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    plan = plan_service.validate(Plan(id="battery-takeover-failure", commands=_commands(device_id)))
    await plan_repository.save_validation(plan)
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(
            owner="domoai", native_scheduler_status="disabled"
        ),
        device_id=device_id,
        command_names=frozenset({"turn_off", "turn_on"}),
        clock=clock,
    )

    summary = await PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        control_takeover=coordinator,
        clock=clock,
    ).execute(plan)

    assert summary.outcomes[0].status.value == "unknown"
    assert summary.outcomes[1].status.value == "rejected"
    assert [call.command for call in adapter.calls] == ["turn_off"]


@pytest.mark.composition
def test_compiler_composition_emits_physical_slot_zero_takeover_command() -> None:
    horizon = energy_horizon(slots=2)
    context = energy_context_for(horizon=horizon)
    assert context.battery is not None
    context = context.model_copy(
        update={
            "battery": context.battery.model_copy(
                update={
                    "actuator": BatteryActuator(
                        device_id="garage.home_battery",
                        capability="battery_power",
                        charge_command="charge_battery",
                        discharge_command="discharge_battery",
                        stop_command="stop_battery",
                        power_feedback_capability="battery_power",
                        power_feedback_tolerance_kw=0.1,
                    )
                }
            )
        }
    )
    scenario = OptimizationScenario(
        id="takeover-compiler-composition",
        horizon=horizon,
        energy_context=context,
    )

    plans = _proposal_plan(
        scenario,
        selected_slots={},
        battery_dispatch_slots={1: (0.0, 2.0)},
    )
    commands = [command for plan in plans for command in plan.commands]

    assert [command.command for command in commands] == [
        "stop_battery",
        "discharge_battery",
        "stop_battery",
    ]
    assert commands[0].intent == "takeover_first_slot:0"


@pytest.mark.composition
@pytest.mark.asyncio
async def test_native_owner_conflict_blocks_all_writes(tmp_path) -> None:
    now = datetime(2026, 8, 23, 12, tzinfo=UTC)
    clock = FixedClock(now)
    adapter = TakeoverFixtureAdapter(clock=clock)
    registry = DeviceRegistry()
    state_store = StateStore(clock=clock)
    audit = AuditLog(clock=clock)
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    plan = plan_service.validate(
        Plan(id="battery-takeover-conflict", commands=_commands(device_id))
    )
    coordinator = BatteryControlCoordinator(
        adapter,
        BatteryControlPolicy(owner="domoai", native_scheduler_status="unknown"),
        device_id=device_id,
        command_names=frozenset({"turn_off", "turn_on"}),
        clock=clock,
    )

    summary = await PlanExecutor(
        adapter, plan_service, audit, control_takeover=coordinator, clock=clock
    ).execute(plan)

    assert all(outcome.status.value == "rejected" for outcome in summary.outcomes)
    assert adapter.calls == []
    assert adapter.control_requests == []
