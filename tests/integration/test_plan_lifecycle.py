import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.executor import PlanExecutor
from domoai.application.plan_service import PlanService
from domoai.application.policy_engine import PolicyEngine
from domoai.application.recovery import PlanRecoveryService
from domoai.application.scheduler import Scheduler
from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.models import (
    AdapterSnapshot,
    Command,
    CommandPostcondition,
    DeviceType,
    ExecutionStatus,
    Plan,
    PlanStatus,
    Policy,
    PolicyAction,
    Precondition,
    RiskClass,
    SafetyLimit,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.persistence.repositories import (
    PlanRepository,
    ScheduledPlanRepository,
    StateSnapshotRepository,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import (
    ApprovalAssertion,
    ApprovalStore,
    OperatorPrincipal,
)
from domoai.runtime.clock import FixedClock
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.risk_classifier import RiskClassifier, RiskOverride
from domoai.runtime.safety_kernel import SafetyKernel
from domoai.runtime.state_store import StateStore
from tests.fixtures.failure_injection import FailureInjectingAdapter
from tests.fixtures.multi_adapter import RecordingAdapter, entity, power_capability


class SettlingFeedbackAdapter(RecordingAdapter):
    def __init__(self, snapshot: AdapterSnapshot, feedback: list[object]) -> None:
        super().__init__("fixture", snapshot)
        self.feedback = feedback
        self.read_count = 0

    async def read_state(self, source_refs):
        self.read_count += 1
        step = self.feedback[min(self.read_count - 1, len(self.feedback) - 1)]
        if isinstance(step, BaseException):
            raise step
        snapshots = await super().read_state(source_refs)
        value, status = step
        return [item.model_copy(update={"value": value, "status": status}) for item in snapshots]


class PostWriteSocAdapter(RecordingAdapter):
    def __init__(self, snapshot: AdapterSnapshot) -> None:
        super().__init__("fixture", snapshot)
        self.read_refs: list[list[str]] = []

    async def read_state(self, source_refs):
        self.read_refs.append([ref.external_id for ref in source_refs])
        snapshots = await super().read_state(source_refs)
        return [
            item.model_copy(update={"value": 2.0})
            if item.capability == "battery_power"
            else item
            for item in snapshots
        ]


class FailingStateSnapshotSink:
    async def save(self, snapshot: StateSnapshot) -> None:
        del snapshot
        raise OSError("simulated state persistence failure")


def reconciliable_battery_snapshot() -> AdapterSnapshot:
    snapshot = settling_battery_snapshot()
    snapshot.source_entities.append(
        {
            "entity_id": "battery.soc",
            "device_id": "battery-device",
            "canonical_id": "battery.home",
            "identity_keys": ["fixture:battery-device"],
            "connections": ["fixture:battery-device"],
            "name": "Battery SOC",
            "area_id": "garage",
            "domain": "sensor",
            "semantic_type": "sensor",
            "capabilities": [
                {
                    "name": "battery.soc",
                    "kind": "number",
                    "unit": "%",
                    "readable": True,
                    "writable": False,
                }
            ],
            "available": True,
        }
    )
    snapshot.source_states.append(
        {
            "entity_id": "battery.soc",
            "capability": "battery.soc",
            "value": 57.0,
            "unit": "%",
            "available": True,
        }
    )
    return snapshot


def settling_battery_snapshot() -> AdapterSnapshot:
    return AdapterSnapshot(
        source_entities=[
            {
                "entity_id": "battery.command",
                "device_id": "battery-device",
                "canonical_id": "battery.home",
                "identity_keys": ["fixture:battery-device"],
                "connections": ["fixture:battery-device"],
                "name": "Battery command",
                "area_id": "garage",
                "domain": "energy",
                "semantic_type": "energy",
                "capabilities": [
                    {
                        "name": "battery_control",
                        "kind": "number",
                        "unit": "kW",
                        "readable": False,
                        "writable": True,
                        "commands": ["charge_battery"],
                    }
                ],
                "available": True,
            },
            {
                "entity_id": "battery.telemetry",
                "device_id": "battery-device",
                "canonical_id": "battery.home",
                "identity_keys": ["fixture:battery-device"],
                "connections": ["fixture:battery-device"],
                "name": "Battery telemetry",
                "area_id": "garage",
                "domain": "sensor",
                "semantic_type": "sensor",
                "capabilities": [
                    {
                        "name": "battery_power",
                        "kind": "number",
                        "unit": "kW",
                        "readable": True,
                        "writable": False,
                    }
                ],
                "available": True,
            },
        ],
        source_states=[
            {
                "entity_id": "battery.telemetry",
                "capability": "battery_power",
                "value": 0.0,
                "unit": "kW",
                "available": True,
            }
        ],
    )


def settling_battery_plan(plan_service: PlanService) -> Plan:
    return plan_service.validate(
        Plan(
            id="battery-feedback-settling",
            commands=[
                Command(
                    id="battery-command-settling",
                    device_id="battery.home",
                    command="charge_battery",
                    value=2.0,
                    unit="kW",
                    idempotency_key="battery-command-settling",
                    postconditions=[
                        CommandPostcondition(
                            capability="battery_power",
                            expected=2.0,
                            tolerance=0.1,
                            settle_timeout_seconds=5.0,
                            poll_interval_seconds=0.25,
                        )
                    ],
                )
            ],
        )
    )


async def build_plan_context() -> tuple[
    SimulatedHomeAdapter, DeviceRegistry, StateStore, AuditLog, PlanService, PlanExecutor
]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    return adapter, registry, state_store, audit, plan_service, executor


async def build_plan_context_with_repository(
    tmp_path,
) -> tuple[
    SimulatedHomeAdapter,
    DeviceRegistry,
    StateStore,
    AuditLog,
    PlanService,
    PlanExecutor,
    PlanRepository,
]:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    executor = PlanExecutor(adapter, plan_service, audit, plan_repository=plan_repository)
    return adapter, registry, state_store, audit, plan_service, executor, plan_repository


@pytest.mark.asyncio
async def test_valid_plan_previews_then_executes() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-light-1",
        commands=[
            Command(
                id="command-light-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-light-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert validated.status is PlanStatus.READY
    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert outcomes.outcomes[0].after_state is not None
    assert outcomes.outcomes[0].after_state.value == 60
    assert [command.id for command in adapter.calls] == ["command-light-1"]


@pytest.mark.asyncio
async def test_battery_feedback_readback_uses_its_own_canonical_route() -> None:
    snapshot = AdapterSnapshot(
        source_entities=[
            {
                "entity_id": "battery.command",
                "device_id": "battery-device",
                "canonical_id": "battery.home",
                "identity_keys": ["fixture:battery-device"],
                "connections": ["fixture:battery-device"],
                "name": "Battery command",
                "area_id": "garage",
                "domain": "energy",
                "semantic_type": "energy",
                "capabilities": [
                    {
                        "name": "battery_control",
                        "kind": "number",
                        "unit": "kW",
                        "readable": False,
                        "writable": True,
                        "commands": ["charge_battery"],
                    }
                ],
                "available": True,
            },
            {
                "entity_id": "battery.telemetry",
                "device_id": "battery-device",
                "canonical_id": "battery.home",
                "identity_keys": ["fixture:battery-device"],
                "connections": ["fixture:battery-device"],
                "name": "Battery telemetry",
                "area_id": "garage",
                "domain": "sensor",
                "semantic_type": "sensor",
                "capabilities": [
                    {
                        "name": "battery_power",
                        "kind": "number",
                        "unit": "kW",
                        "readable": True,
                        "writable": False,
                    }
                ],
                "available": True,
            },
        ],
        source_states=[
            {
                "entity_id": "battery.telemetry",
                "capability": "battery_power",
                "value": 2.05,
                "unit": "kW",
                "available": True,
            }
        ],
    )
    adapter = RecordingAdapter("fixture", snapshot)
    await adapter.connect()
    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    state_store = StateStore()
    audit = AuditLog()
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [],
            RiskClassifier(
                overrides=(
                    RiskOverride(
                        device_id="battery.home",
                        risk_class=RiskClass.SAFE,
                        privileged_exception=True,
                    ),
                )
            ),
        ),
        audit,
        authorized_actuator_commands={
            "battery.home": frozenset(
                {"charge_battery", "discharge_battery", "stop_battery"}
            )
        },
    )
    executor = PlanExecutor(adapter, plan_service, audit)
    plan = Plan(
        id="battery-feedback-readback",
        commands=[
            Command(
                id="battery-command",
                device_id="battery.home",
                command="charge_battery",
                value=2.0,
                unit="kW",
                idempotency_key="battery-command",
                postconditions=[
                    CommandPostcondition(
                        capability="battery_power", expected=2.0, tolerance=0.1
                    )
                ],
            )
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert summary.outcomes[0].after_state is not None
    assert summary.outcomes[0].after_state.source_ref.external_id == "battery.telemetry"


@pytest.mark.asyncio
async def test_post_write_soc_readback_is_persisted_without_replaying_write(tmp_path) -> None:
    snapshot = reconciliable_battery_snapshot()
    adapter = PostWriteSocAdapter(snapshot)
    await adapter.connect()
    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    state_store = StateStore()
    audit = AuditLog()
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [],
            RiskClassifier(
                overrides=(
                    RiskOverride(
                        device_id="battery.home",
                        risk_class=RiskClass.SAFE,
                        privileged_exception=True,
                    ),
                )
            ),
        ),
        audit,
        authorized_actuator_commands={
            "battery.home": frozenset(
                {"charge_battery", "discharge_battery", "stop_battery"}
            )
        },
    )
    database = SQLiteDatabase(tmp_path / "soc-reconciliation.sqlite3")
    await database.initialize()
    state_repository = StateSnapshotRepository(database)
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        state_snapshot_repository=state_repository,
    )
    plan = Plan(
        id="battery-soc-reconciliation",
        commands=[
            Command(
                id="battery-command-reconcile",
                device_id="battery.home",
                command="charge_battery",
                value=2.0,
                unit="kW",
                idempotency_key="battery-command-reconcile",
                postconditions=[
                    CommandPostcondition(
                        capability="battery_power",
                        expected=2.0,
                        tolerance=0.1,
                        reconcile_capabilities=["battery.soc"],
                    )
                ],
            )
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert len(adapter.writes) == 1
    assert adapter.read_refs == [["battery.telemetry"], ["battery.soc"]]
    persisted = await state_repository.list_all()
    soc = next(item for item in persisted if item.capability == "battery.soc")
    assert soc.value == 57.0
    assert soc.status is StateStatus.CURRENT
    restored_store = StateStore()
    restored_store.load_persisted(persisted)
    restored_soc = await restored_store.get("battery.home", "battery.soc")
    assert restored_soc is not None
    assert restored_soc.status is StateStatus.STALE


@pytest.mark.asyncio
async def test_explicit_soc_reconciliation_route_is_required_before_execution() -> None:
    snapshot = settling_battery_snapshot()
    adapter = PostWriteSocAdapter(snapshot)
    await adapter.connect()
    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    state_store = StateStore()
    audit = AuditLog()
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [],
            RiskClassifier(
                overrides=(
                    RiskOverride(
                        device_id="battery.home",
                        risk_class=RiskClass.SAFE,
                        privileged_exception=True,
                    ),
                )
            ),
        ),
        audit,
        authorized_actuator_commands={
            "battery.home": frozenset(
                {"charge_battery", "discharge_battery", "stop_battery"}
            )
        },
    )
    plan = Plan(
        id="battery-soc-route-required",
        commands=[
            Command(
                id="battery-command-route-required",
                device_id="battery.home",
                command="charge_battery",
                value=2.0,
                unit="kW",
                idempotency_key="battery-command-route-required",
                postconditions=[
                    CommandPostcondition(
                        capability="battery_power",
                        expected=2.0,
                        tolerance=0.1,
                        reconcile_capabilities=["battery.soc"],
                    )
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)

    assert any(
        error.code in {ErrorCode.INVALID_CAPABILITY, ErrorCode.ROUTE_NOT_FOUND}
        for error in validated.validation.errors
    )
    with pytest.raises(DomainError) as excinfo:
        await PlanExecutor(adapter, plan_service, audit).execute(validated)
    assert excinfo.value.code == ErrorCode.VALIDATION_ERROR
    assert adapter.writes == []


@pytest.mark.asyncio
async def test_readback_persistence_failure_is_unknown_without_write_replay() -> None:
    snapshot = settling_battery_snapshot()
    adapter = PostWriteSocAdapter(snapshot)
    await adapter.connect()
    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    state_store = StateStore()
    audit = AuditLog()
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [],
            RiskClassifier(
                overrides=(
                    RiskOverride(
                        device_id="battery.home",
                        risk_class=RiskClass.SAFE,
                        privileged_exception=True,
                    ),
                )
            ),
        ),
        audit,
        authorized_actuator_commands={
            "battery.home": frozenset(
                {"charge_battery", "discharge_battery", "stop_battery"}
            )
        },
    )
    plan = Plan(
        id="battery-readback-persistence-failure",
        commands=[
            Command(
                id="battery-command-persistence-failure",
                device_id="battery.home",
                command="charge_battery",
                value=2.0,
                unit="kW",
                idempotency_key="battery-command-persistence-failure",
                postconditions=[
                    CommandPostcondition(
                        capability="battery_power",
                        expected=2.0,
                        tolerance=0.1,
                    )
                ],
            )
        ],
    )

    summary = await PlanExecutor(
        adapter,
        plan_service,
        audit,
        state_snapshot_repository=FailingStateSnapshotSink(),
    ).execute(plan_service.validate(plan))

    assert summary.outcomes[0].status is ExecutionStatus.UNKNOWN
    assert summary.outcomes[0].error is not None
    assert summary.outcomes[0].error.code == ErrorCode.POST_WRITE_RECONCILIATION_FAILED
    assert len(adapter.writes) == 1


async def _build_settling_context(
    feedback: list[object],
    *,
    sleep=None,
) -> tuple[SettlingFeedbackAdapter, PlanService, PlanExecutor, FixedClock]:
    snapshot = settling_battery_snapshot()
    adapter = SettlingFeedbackAdapter(snapshot, feedback)
    await adapter.connect()
    registry = DeviceRegistry()
    registry.apply_snapshot(snapshot, adapter.adapter_id)
    state_store = StateStore()
    audit = AuditLog()
    plan_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [],
            RiskClassifier(
                overrides=(
                    RiskOverride(
                        device_id="battery.home",
                        risk_class=RiskClass.SAFE,
                        privileged_exception=True,
                    ),
                )
            ),
        ),
        audit,
        authorized_actuator_commands={
            "battery.home": frozenset(
                {"charge_battery", "discharge_battery", "stop_battery"}
            )
        },
    )
    clock = FixedClock(datetime.now(UTC))

    async def advance(delay: float) -> None:
        clock.set(clock.now() + timedelta(seconds=delay))

    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        clock=clock,
        sleep=sleep or advance,
    )
    return adapter, plan_service, executor, clock


@pytest.mark.asyncio
async def test_battery_feedback_settling_confirms_late_without_replaying_write() -> None:
    old = (0.0, StateStatus.CURRENT)
    matching = (2.05, StateStatus.CURRENT)
    adapter, plan_service, executor, _clock = await _build_settling_context([old, matching])

    summary = await executor.execute(settling_battery_plan(plan_service))

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert adapter.read_count == 2
    assert len(adapter.writes) == 1


@pytest.mark.asyncio
async def test_battery_feedback_settling_deadline_preserves_latest_observation() -> None:
    adapter, plan_service, executor, _clock = await _build_settling_context(
        [(0.0, StateStatus.CURRENT)]
    )

    summary = await executor.execute(settling_battery_plan(plan_service))

    assert summary.outcomes[0].status is ExecutionStatus.UNKNOWN
    assert summary.outcomes[0].after_state is not None
    assert summary.outcomes[0].after_state.value == 0.0
    assert len(adapter.writes) == 1
    assert adapter.read_count > 1


@pytest.mark.asyncio
async def test_battery_feedback_settling_retries_transient_readback_failure() -> None:
    adapter, plan_service, executor, _clock = await _build_settling_context(
        [TimeoutError("telemetry delayed"), (2.0, StateStatus.CURRENT)]
    )

    summary = await executor.execute(settling_battery_plan(plan_service))

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert adapter.read_count == 2
    assert len(adapter.writes) == 1


@pytest.mark.asyncio
async def test_battery_feedback_settling_does_not_confirm_stale_feedback() -> None:
    adapter, plan_service, executor, _clock = await _build_settling_context(
        [(2.0, StateStatus.STALE)]
    )

    summary = await executor.execute(settling_battery_plan(plan_service))

    assert summary.outcomes[0].status is ExecutionStatus.UNKNOWN
    assert summary.outcomes[0].after_state is not None
    assert summary.outcomes[0].after_state.status is StateStatus.STALE
    assert len(adapter.writes) == 1


@pytest.mark.asyncio
async def test_battery_feedback_settling_propagates_cancellation_without_replay() -> None:
    async def cancel(_delay: float) -> None:
        raise asyncio.CancelledError

    adapter, plan_service, executor, _clock = await _build_settling_context(
        [(0.0, StateStatus.CURRENT)], sleep=cancel
    )

    with pytest.raises(asyncio.CancelledError):
        await executor.execute(settling_battery_plan(plan_service))

    assert len(adapter.writes) == 1


@pytest.mark.asyncio
async def test_plan_preflight_rejects_later_safety_violation_before_first_write() -> None:
    adapter, registry, _, audit, plan_service, _ = await build_plan_context()
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    climate_id = next(device.id for device in registry.devices if device.type.value == "climate")
    plan = Plan(
        id="plan-preflight-safety-1",
        commands=[
            Command(
                id="command-preflight-safe-1",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="intent-preflight-safe-1",
            ),
            Command(
                id="command-preflight-unsafe-1",
                device_id=climate_id,
                command="set_temperature",
                value=25,
                unit="°C",
                idempotency_key="intent-preflight-unsafe-1",
            ),
        ],
    )
    validated = plan_service.validate(plan)
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        safety_kernel=SafetyKernel(
            [
                SafetyLimit(
                    device_type=DeviceType.CLIMATE,
                    capability="target_temperature",
                    maximum=22,
                )
            ]
        ),
    )

    summary = await executor.execute(validated)

    assert adapter.calls == []
    assert [outcome.status for outcome in summary.outcomes] == [
        ExecutionStatus.REJECTED,
        ExecutionStatus.REJECTED,
    ]
    assert summary.outcomes[1].error is not None
    assert summary.outcomes[1].error.code == ErrorCode.SAFETY_LIMIT_EXCEEDED
    assert any(event.event_type == "plan_preflight_rejected" for event in audit.events)


@pytest.mark.asyncio
async def test_plan_preflight_rejects_later_unmet_precondition_before_first_write() -> None:
    adapter, registry, _, audit, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-preflight-precondition-1",
        commands=[
            Command(
                id="command-preflight-first-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-preflight-first-1",
            ),
            Command(
                id="command-preflight-second-1",
                device_id=light_id,
                command="set_brightness",
                value=70,
                unit="%",
                idempotency_key="intent-preflight-second-1",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=True)
                ],
            ),
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert adapter.calls == []
    assert [outcome.status for outcome in summary.outcomes] == [
        ExecutionStatus.REJECTED,
        ExecutionStatus.REJECTED,
    ]
    assert summary.outcomes[1].error is not None
    assert summary.outcomes[1].error.code == ErrorCode.PRECONDITION_FAILED


@pytest.mark.asyncio
async def test_preflight_rejection_is_persisted_terminal_and_not_replayed(tmp_path) -> None:
    adapter, registry, _, audit, plan_service, _, plan_repository = (
        await build_plan_context_with_repository(tmp_path)
    )
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    climate_id = next(device.id for device in registry.devices if device.type.value == "climate")
    plan = Plan(
        id="plan-preflight-persisted-1",
        commands=[
            Command(
                id="command-preflight-persisted-safe-1",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="intent-preflight-persisted-safe-1",
            ),
            Command(
                id="command-preflight-persisted-unsafe-1",
                device_id=climate_id,
                command="set_temperature",
                value=25,
                unit="°C",
                idempotency_key="intent-preflight-persisted-unsafe-1",
            ),
        ],
    )
    validated = plan_service.validate(plan)
    executor = PlanExecutor(
        adapter,
        plan_service,
        audit,
        plan_repository=plan_repository,
        safety_kernel=SafetyKernel(
            [
                SafetyLimit(
                    device_type=DeviceType.CLIMATE,
                    capability="target_temperature",
                    maximum=22,
                )
            ]
        ),
    )

    summary = await executor.execute(validated)

    persisted = await plan_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.FAILED
    assert persisted.execution == summary
    with pytest.raises(ValueError, match="already|terminal"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_plan_preflight_preserves_deterministic_sequential_precondition() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-preflight-sequence-1",
        commands=[
            Command(
                id="command-preflight-sequence-on",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="intent-preflight-sequence-on",
            ),
            Command(
                id="command-preflight-sequence-brightness",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-preflight-sequence-brightness",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=True)
                ],
            ),
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert [outcome.status for outcome in summary.outcomes] == [
        ExecutionStatus.CONFIRMED_SUCCESS,
        ExecutionStatus.CONFIRMED_SUCCESS,
    ]
    assert [command.id for command in adapter.calls] == [
        "command-preflight-sequence-on",
        "command-preflight-sequence-brightness",
    ]


@pytest.mark.asyncio
async def test_just_in_time_precondition_still_blocks_state_race_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    switch_state = await state_store.get(switch_id, "power")
    assert switch_state is not None
    await state_store.save(switch_state.model_copy(update={"value": True}))
    first_command_id = "command-preflight-race-first"
    plan = Plan(
        id="plan-preflight-race-1",
        commands=[
            Command(
                id=first_command_id,
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-preflight-race-first",
            ),
            Command(
                id="command-preflight-race-second",
                device_id=light_id,
                command="set_brightness",
                value=70,
                unit="%",
                idempotency_key="intent-preflight-race-second",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=True)
                ],
            ),
        ],
    )
    original_execute = adapter.execute

    async def execute_and_change_state(command: Command, execution_context=None):
        acknowledgement = await original_execute(command, execution_context)
        if command.id == first_command_id:
            current = await state_store.get(switch_id, "power")
            assert current is not None
            await state_store.save(current.model_copy(update={"value": False}))
        return acknowledgement

    monkeypatch.setattr(adapter, "execute", execute_and_change_state)
    summary = await executor.execute(plan_service.validate(plan))

    assert [outcome.status for outcome in summary.outcomes] == [
        ExecutionStatus.CONFIRMED_SUCCESS,
        ExecutionStatus.REJECTED,
    ]
    assert [command.id for command in adapter.calls] == [first_command_id]


@pytest.mark.asyncio
async def test_out_of_range_command_is_rejected_before_adapter_call() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-invalid-1",
        commands=[
            Command(
                id="command-invalid-1",
                device_id=device_id,
                command="set_brightness",
                value=140,
                unit="%",
                idempotency_key="intent-invalid-1",
            )
        ],
    )

    validated = plan_service.validate(plan)

    assert validated.status is PlanStatus.VALIDATED
    assert validated.validation is not None
    assert validated.validation.status.value == "invalid"
    with pytest.raises(ValueError):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_sensitive_command_requires_matching_operator_approval() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    plan = Plan(
        id="plan-cover-1",
        commands=[
            Command(
                id="command-cover-1",
                device_id=device_id,
                command="open",
                risk_class=RiskClass.CONFIRM,
                idempotency_key="intent-cover-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    assert validated.status is PlanStatus.REQUIRES_CONFIRMATION
    with pytest.raises(ValueError):
        await executor.execute(validated)
    assert adapter.calls == []

    grant = ApprovalStore(
        operator_token="test-operator-secret", allow_legacy_token=True
    ).issue(
        validated, approved_by="local_operator", operator_token="test-operator-secret"
    )
    approved = plan_service.approve(validated, grant=grant)
    outcomes = await executor.execute(approved)

    assert approved.status is PlanStatus.APPROVED
    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_changed_runtime_revision_requires_revalidation() -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-stale-1",
        commands=[
            Command(
                id="command-stale-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-stale-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    state_store.begin_revision()

    with pytest.raises(ValueError, match="runtime revision"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_changed_capability_bound_requires_revalidation_before_adapter_write() -> None:
    adapter, registry, state_store, audit, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-capability-stale-1",
        commands=[
            Command(
                id="command-capability-stale-1",
                device_id=device_id,
                command="set_brightness",
                value=90,
                unit="%",
                idempotency_key="intent-capability-stale-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    next(entity for entity in adapter._entities if entity["entity_id"] == "light.living_room_main")[
        "attributes"
    ]["brightness_max"] = 50
    await DiscoveryService(adapter, registry, state_store, audit).refresh()

    with pytest.raises(ValueError, match="stale"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_legacy_validation_without_dependency_evidence_fails_closed() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-legacy-dependencies-1",
        commands=[
            Command(
                id="command-legacy-dependencies-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-legacy-dependencies-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    assert validated.validation is not None
    legacy = validated.model_copy(
        update={"validation": validated.validation.model_copy(update={"dependencies": None})}
    )

    with pytest.raises(ValueError, match="stale"):
        await executor.execute(legacy)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_state_marked_stale_in_background_forces_revalidation() -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    initial_state = await state_store.get(device_id, "brightness")
    assert initial_state is not None
    plan = Plan(
        id="plan-background-stale-1",
        commands=[
            Command(
                id="command-background-stale-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-background-stale-1",
                preconditions=[
                    Precondition(
                        device_id=device_id,
                        capability="brightness",
                        expected=initial_state.value,
                    )
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    await state_store.mark_all_stale()

    with pytest.raises(ValueError, match="stale"):
        await executor.execute(validated)
    assert adapter.calls == []


def _shared_device_snapshot(
    *, entity_id: str, source_device_id: str, value: bool, include_target: bool = False
) -> AdapterSnapshot:
    shared_entity = entity(
        entity_id=entity_id,
        source_device_id=source_device_id,
        canonical_id="shared.state_conflict_device",
        name="Shared Power",
        capabilities=[power_capability()],
    )
    entities = [shared_entity]
    states = [
        {
            "entity_id": entity_id,
            "capability": "power",
            "value": value,
            "unit": None,
            "available": True,
        }
    ]
    if include_target:
        target_entity = entity(
            entity_id="light.target",
            source_device_id="physical-target-1",
            canonical_id="light.conflict_precondition_target",
            name="Target Light",
            capabilities=[power_capability()],
        )
        entities.append(target_entity)
        states.append(
            {
                "entity_id": "light.target",
                "capability": "power",
                "value": False,
                "unit": None,
                "available": True,
            }
        )
    return AdapterSnapshot(source_entities=entities, source_states=states)


@pytest.mark.asyncio
async def test_precondition_on_conflicted_capability_is_refused_without_adapter_call() -> None:
    first = RecordingAdapter(
        "home_assistant",
        _shared_device_snapshot(
            entity_id="light.power_a",
            source_device_id="physical-shared-1",
            value=True,
            include_target=True,
        ),
    )
    second = RecordingAdapter(
        "modbus",
        _shared_device_snapshot(
            entity_id="modbus.power_b", source_device_id="modbus-physical-shared-1", value=False
        ),
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    await composite.connect()
    await DiscoveryService(composite, registry, state_store, audit).refresh()

    cached = await state_store.get("shared.state_conflict_device", "power")
    assert cached is not None
    assert cached.status is StateStatus.INVALID

    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(composite, plan_service, audit)
    plan = Plan(
        id="plan-conflict-precondition-1",
        commands=[
            Command(
                id="command-conflict-precondition-1",
                device_id="light.conflict_precondition_target",
                command="turn_on",
                idempotency_key="intent-conflict-precondition-1",
                preconditions=[
                    Precondition(
                        device_id="shared.state_conflict_device",
                        capability="power",
                        expected=True,
                    )
                ],
            )
        ],
    )
    validated = plan_service.validate(plan)
    summary = await executor.execute(validated)

    assert summary.outcomes[0].status is ExecutionStatus.REJECTED
    assert summary.outcomes[0].error is not None
    assert summary.outcomes[0].error.code == ErrorCode.PRECONDITION_FAILED
    assert first.writes == []
    assert second.writes == []


@pytest.mark.asyncio
async def test_precondition_expecting_none_is_still_unmet_against_invalid_state() -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    invalid_snapshot = StateSnapshot(
        device_id="shared.conflicted_device",
        capability="power",
        value=None,
        observed_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        status=StateStatus.INVALID,
        source_ref=SourceRef(adapter_id="modbus", external_id="modbus.power_b"),
    )
    await state_store.save(invalid_snapshot)
    plan = Plan(
        id="plan-invalid-precondition-expects-none-1",
        commands=[
            Command(
                id="command-invalid-precondition-expects-none-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-invalid-precondition-expects-none-1",
                preconditions=[
                    Precondition(
                        device_id="shared.conflicted_device", capability="power", expected=None
                    )
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    summary = await executor.execute(validated)

    assert summary.outcomes[0].status is ExecutionStatus.REJECTED
    assert summary.outcomes[0].error is not None
    assert summary.outcomes[0].error.code == ErrorCode.PRECONDITION_FAILED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_conflict_resolving_on_later_discovery_restores_current_status() -> None:
    first = RecordingAdapter(
        "home_assistant",
        _shared_device_snapshot(
            entity_id="light.power_a", source_device_id="physical-shared-1", value=True
        ),
    )
    second = RecordingAdapter(
        "modbus",
        _shared_device_snapshot(
            entity_id="modbus.power_b", source_device_id="modbus-physical-shared-1", value=False
        ),
    )
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    state_store = StateStore()
    audit = AuditLog()
    discovery = DiscoveryService(composite, registry, state_store, audit)
    await composite.connect()
    await discovery.refresh()

    cached = await state_store.get("shared.state_conflict_device", "power")
    assert cached is not None
    assert cached.status is StateStatus.INVALID

    second.snapshot = _shared_device_snapshot(
        entity_id="modbus.power_b", source_device_id="modbus-physical-shared-1", value=True
    )
    await discovery.refresh()

    resolved = await state_store.get("shared.state_conflict_device", "power")
    assert resolved is not None
    assert resolved.status is StateStatus.CURRENT
    assert resolved.value is True


@pytest.mark.asyncio
async def test_safety_kernel_blocks_command_allowed_by_capability_and_policy() -> None:
    adapter, registry, _, audit, plan_service, _ = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "climate")
    plan = Plan(
        id="plan-safety-kernel-1",
        commands=[
            Command(
                id="command-safety-kernel-1",
                device_id=device_id,
                command="set_temperature",
                value=25,
                unit="°C",
                idempotency_key="intent-safety-kernel-1",
            )
        ],
    )
    validated = plan_service.validate(plan)
    assert validated.status is PlanStatus.READY

    safety_kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.CLIMATE, capability="target_temperature", maximum=22)]
    )
    executor = PlanExecutor(adapter, plan_service, audit, safety_kernel=safety_kernel)

    summary = await executor.execute(validated)

    assert summary.outcomes[0].status is ExecutionStatus.REJECTED
    assert summary.outcomes[0].error is not None
    assert summary.outcomes[0].error.code == ErrorCode.SAFETY_LIMIT_EXCEEDED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_unconfigured_safety_kernel_leaves_execution_unaffected() -> None:
    adapter, registry, _, audit, plan_service, _ = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "climate")
    plan = Plan(
        id="plan-safety-kernel-2",
        commands=[
            Command(
                id="command-safety-kernel-2",
                device_id=device_id,
                command="set_temperature",
                value=25,
                unit="°C",
                idempotency_key="intent-safety-kernel-2",
            )
        ],
    )
    validated = plan_service.validate(plan)

    executor_without_kernel = PlanExecutor(adapter, plan_service, audit)
    summary = await executor_without_kernel.execute(validated)
    assert summary.outcomes[0].status is not ExecutionStatus.REJECTED

    non_matching_kernel = SafetyKernel(
        [SafetyLimit(device_type=DeviceType.COVER, capability="position", maximum=50)]
    )
    executor_with_non_matching_kernel = PlanExecutor(
        adapter, plan_service, audit, safety_kernel=non_matching_kernel
    )
    plan2 = plan.model_copy(
        update={
            "id": "plan-safety-kernel-3",
            "commands": [
                Command(
                    id="command-safety-kernel-3",
                    device_id=device_id,
                    command="set_temperature",
                    value=25,
                    unit="°C",
                    idempotency_key="intent-safety-kernel-3",
                )
            ],
        }
    )
    validated2 = plan_service.validate(plan2)
    summary2 = await executor_with_non_matching_kernel.execute(validated2)
    assert summary2.outcomes[0].status is not ExecutionStatus.REJECTED


@pytest.mark.asyncio
async def test_cancelled_plan_never_reaches_adapter() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-cancelled-1",
        commands=[
            Command(
                id="command-cancelled-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-cancelled-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    cancelled = plan_service.cancel(validated)

    assert cancelled.status is PlanStatus.CANCELLED
    with pytest.raises(ValueError, match="cancelled"):
        await executor.execute(cancelled)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_precondition_unmet_rejects_without_adapter_call() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-precondition-unmet-1",
        commands=[
            Command(
                id="command-precondition-unmet-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-precondition-unmet-1",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=True)
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    outcome = outcomes.outcomes[0]
    assert outcome.status.value == "rejected"
    assert outcome.error is not None
    assert outcome.error.code == "precondition_failed"
    assert len(outcome.error.details["preconditions"]) == 1
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_precondition_with_no_known_state_is_treated_as_unmet() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-precondition-unknown-1",
        commands=[
            Command(
                id="command-precondition-unknown-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-precondition-unknown-1",
                preconditions=[
                    Precondition(device_id="nonexistent.device", capability="soc", expected=60)
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    outcome = outcomes.outcomes[0]
    assert outcome.status.value == "rejected"
    assert outcome.error is not None
    assert outcome.error.code == "precondition_failed"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_precondition_met_behaves_like_no_precondition() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-precondition-met-1",
        commands=[
            Command(
                id="command-precondition-met-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-precondition-met-1",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=False)
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    outcome = outcomes.outcomes[0]
    assert outcome.status.value == "confirmed_success"
    assert [command.id for command in adapter.calls] == ["command-precondition-met-1"]


@pytest.mark.asyncio
async def test_precondition_outcome_lists_every_unsatisfied_precondition() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-precondition-multi-1",
        commands=[
            Command(
                id="command-precondition-multi-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-precondition-multi-1",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=False),
                    Precondition(device_id="nonexistent.device", capability="soc", expected=60),
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    outcome = outcomes.outcomes[0]
    assert outcome.status.value == "rejected"
    assert outcome.error is not None
    failed = outcome.error.details["preconditions"]
    assert len(failed) == 1
    assert failed[0]["device_id"] == "nonexistent.device"
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_plan_with_only_precondition_failures_completes_without_error() -> None:
    _, _, _, _, plan_service, executor = await build_plan_context()
    registry = plan_service.registry
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-precondition-all-fail-1",
        commands=[
            Command(
                id="command-precondition-all-fail-1",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-precondition-all-fail-1",
                preconditions=[
                    Precondition(device_id="nonexistent.device", capability="soc", expected=60)
                ],
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert len(outcomes.outcomes) == 1
    assert outcomes.outcomes[0].status.value == "rejected"


@pytest.mark.asyncio
async def test_precondition_sequencing_sees_earlier_command_confirmed_effect() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-precondition-sequencing-1",
        commands=[
            Command(
                id="command-precondition-sequencing-turn-on",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="intent-precondition-sequencing-turn-on",
            ),
            Command(
                id="command-precondition-sequencing-brightness",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-precondition-sequencing-brightness",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=True)
                ],
            ),
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert [outcome.status.value for outcome in outcomes.outcomes] == [
        "confirmed_success",
        "confirmed_success",
    ]
    assert [command.id for command in adapter.calls] == [
        "command-precondition-sequencing-turn-on",
        "command-precondition-sequencing-brightness",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot_status", "should_execute"),
    [
        (StateStatus.CURRENT, True),
        (StateStatus.STALE, False),
        (StateStatus.UNAVAILABLE, False),
        (StateStatus.INVALID, False),
    ],
)
async def test_physical_precondition_requires_current_evidence(
    snapshot_status: StateStatus, should_execute: bool
) -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    source = await state_store.get(switch_id, "power")
    assert source is not None
    await state_store.save(source.model_copy(update={"status": snapshot_status}))
    plan = Plan(
        id=f"plan-precondition-freshness-{snapshot_status.value}",
        commands=[
            Command(
                id=f"command-precondition-freshness-{snapshot_status.value}",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key=f"intent-precondition-freshness-{snapshot_status.value}",
                preconditions=[
                    Precondition(
                        device_id=switch_id,
                        capability="power",
                        expected=source.value,
                    )
                ],
            )
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert bool(adapter.calls) is should_execute
    assert summary.outcomes[0].status is (
        ExecutionStatus.CONFIRMED_SUCCESS if should_execute else ExecutionStatus.REJECTED
    )


@pytest.mark.asyncio
async def test_explicit_stale_exception_is_policy_bound_and_audited() -> None:
    adapter, registry, state_store, audit, _, _ = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    source = await state_store.get(switch_id, "power")
    assert source is not None
    await state_store.save(source.model_copy(update={"status": StateStatus.STALE}))
    policy_service = PlanService(
        registry,
        state_store,
        PolicyEngine(
            [
                Policy(
                    id="policy-explicit-stale",
                    action=PolicyAction.ALLOW,
                    target={"device_id": light_id, "command": "set_brightness"},
                    allows_stale=True,
                )
            ]
        ),
        audit,
    )
    executor = PlanExecutor(adapter, policy_service, audit)
    plan = Plan(
        id="plan-explicit-stale-exception",
        commands=[
            Command(
                id="command-explicit-stale-exception",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-explicit-stale-exception",
                preconditions=[
                    Precondition(
                        device_id=switch_id,
                        capability="power",
                        expected=source.value,
                        allow_stale=True,
                    )
                ],
            )
        ],
    )

    summary = await executor.execute(policy_service.validate(plan))

    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    exception_events = [
        event
        for event in audit.events
        if event.event_type == "precondition_stale_exception"
    ]
    assert exception_events
    assert exception_events[0].payload["policy_id"] == "policy-explicit-stale"
    assert exception_events[0].payload["authority"] == "policy-engine"


@pytest.mark.asyncio
async def test_stale_projected_state_cannot_authorize_later_command() -> None:
    adapter, registry, state_store, _, plan_service, executor = await build_plan_context()
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    source = await state_store.get(switch_id, "power")
    assert source is not None
    await state_store.save(source.model_copy(update={"status": StateStatus.STALE}))
    plan = Plan(
        id="plan-stale-projection-composition",
        commands=[
            Command(
                id="command-stale-projection-on",
                device_id=switch_id,
                command="turn_on",
                idempotency_key="intent-stale-projection-on",
            ),
            Command(
                id="command-stale-projection-brightness",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-stale-projection-brightness",
                preconditions=[
                    Precondition(device_id=switch_id, capability="power", expected=True)
                ],
            ),
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert summary.outcomes[0].status is ExecutionStatus.REJECTED
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_jit_rejects_precondition_that_becomes_stale_after_preflight() -> None:
    state_store = StateStore()

    class StalingAdapter(SimulatedHomeAdapter):
        async def execute(self, command, execution_context=None):
            if not self.calls:
                snapshot = await state_store.get(switch_id, "power")
                assert snapshot is not None
                await state_store.save(snapshot.model_copy(update={"status": StateStatus.STALE}))
            return await super().execute(command, execution_context)

    adapter = StalingAdapter()
    registry = DeviceRegistry()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    executor = PlanExecutor(adapter, plan_service, audit)
    light_id = next(device.id for device in registry.devices if device.type.value == "light")
    switch_id = next(device.id for device in registry.devices if device.type.value == "switch")
    source = await state_store.get(switch_id, "power")
    assert source is not None
    plan = Plan(
        id="plan-jit-freshness-race",
        commands=[
            Command(
                id="command-jit-freshness-first",
                device_id=light_id,
                command="turn_on",
                idempotency_key="intent-jit-freshness-first",
            ),
            Command(
                id="command-jit-freshness-second",
                device_id=light_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-jit-freshness-second",
                preconditions=[
                    Precondition(
                        device_id=switch_id,
                        capability="power",
                        expected=source.value,
                    )
                ],
            ),
        ],
    )

    summary = await executor.execute(plan_service.validate(plan))

    assert len(adapter.calls) == 1
    assert summary.outcomes[0].status is ExecutionStatus.CONFIRMED_SUCCESS
    assert summary.outcomes[1].status is ExecutionStatus.REJECTED


@pytest.mark.asyncio
async def test_double_execution_of_terminal_plan_is_refused(tmp_path) -> None:
    adapter, registry, _, _, plan_service, executor, _ = await build_plan_context_with_repository(
        tmp_path
    )
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-double-execution-1",
        commands=[
            Command(
                id="command-double-execution-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-double-execution-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    await executor.execute(validated)
    assert len(adapter.calls) == 1

    with pytest.raises(ValueError, match="invalid_transition|already"):
        await executor.execute(validated)
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_double_execution_of_in_progress_plan_is_refused(tmp_path) -> None:
    (
        adapter,
        registry,
        _,
        _,
        plan_service,
        executor,
        plan_repository,
    ) = await build_plan_context_with_repository(tmp_path)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-double-execution-2",
        commands=[
            Command(
                id="command-double-execution-2",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-double-execution-2",
            )
        ],
    )

    validated = plan_service.validate(plan)
    await plan_repository.save(validated.model_copy(update={"status": PlanStatus.EXECUTING}))

    with pytest.raises(ValueError, match="invalid_transition|already"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_plan_recovered_from_crash_can_never_be_claimed_again(tmp_path) -> None:
    (
        adapter,
        registry,
        _,
        audit,
        plan_service,
        executor,
        plan_repository,
    ) = await build_plan_context_with_repository(tmp_path)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-crash-recovery-1",
        commands=[
            Command(
                id="command-crash-recovery-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-crash-recovery-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    await plan_repository.save(validated.model_copy(update={"status": PlanStatus.EXECUTING}))

    service = PlanRecoveryService(plan_repository, audit)
    recovered_ids = await service.recover_orphaned_plans()
    assert recovered_ids == [plan.id]

    with pytest.raises(ValueError, match="invalid_transition|already"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_first_time_execution_with_repository_is_unaffected(tmp_path) -> None:
    adapter, registry, _, _, plan_service, executor, _ = await build_plan_context_with_repository(
        tmp_path
    )
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-first-execution-1",
        commands=[
            Command(
                id="command-first-execution-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-first-execution-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert len(adapter.calls) == 1


class _InterleavingPlanRepository:
    """Wraps a real ``PlanRepository`` and forces a genuine asyncio
    scheduler yield (``asyncio.sleep(0)``) before every call.

    The fixture adapter and the ``sqlite3`` driver used throughout this
    suite are fully synchronous, so two ``execute()`` calls dispatched via
    ``asyncio.gather`` never naturally interleave — one runs to completion
    before the other starts, which would make a concurrency test pass
    "by accident" regardless of whether the claim is actually atomic. This
    wrapper reproduces the adversarial interleaving a real, network-backed
    adapter's genuine ``await`` points would produce, so the test exercises
    the property that matters: whichever caller's claim attempt is
    evaluated first wins, and every other simultaneous attempt is refused
    — not merely "whichever attempt started first finishes first."
    """

    def __init__(self, inner: PlanRepository) -> None:
        self._inner = inner

    async def get(self, plan_id: str) -> Plan | None:
        await asyncio.sleep(0)
        return await self._inner.get(plan_id)

    async def save(self, plan: Plan) -> None:
        await asyncio.sleep(0)
        await self._inner.save(plan)

    async def save_approval(self, plan: Plan) -> None:
        await asyncio.sleep(0)
        await self._inner.save_approval(plan)

    async def settle_execution(self, plan: Plan) -> None:
        await asyncio.sleep(0)
        await self._inner.settle_execution(plan)

    async def claim_for_execution(
        self, plan: Plan, *, allowed_statuses: frozenset[PlanStatus]
    ) -> bool:
        await asyncio.sleep(0)
        return await self._inner.claim_for_execution(plan, allowed_statuses=allowed_statuses)


@pytest.mark.asyncio
async def test_two_concurrent_execution_attempts_on_the_same_plan_have_exactly_one_winner(
    tmp_path,
) -> None:
    (
        adapter,
        registry,
        _,
        _,
        plan_service,
        executor,
        plan_repository,
    ) = await build_plan_context_with_repository(tmp_path)
    executor.plan_repository = _InterleavingPlanRepository(plan_repository)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-concurrent-execution-1",
        commands=[
            Command(
                id="command-concurrent-execution-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-concurrent-execution-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    results = await asyncio.gather(
        executor.execute(validated), executor.execute(validated), return_exceptions=True
    )

    successes = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "invalid_transition" in str(failures[0]) or "already" in str(failures[0])
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_future_execute_at_is_refused_without_adapter_call() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-not-yet-due-1",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        commands=[
            Command(
                id="command-not-yet-due-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-not-yet-due-1",
            )
        ],
    )

    validated = plan_service.validate(plan)

    with pytest.raises(ValueError, match="not_yet_due|not yet due"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_past_execute_at_runs_normally() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-already-due-1",
        execute_at=datetime.now(UTC) - timedelta(minutes=1),
        commands=[
            Command(
                id="command-already-due-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-already-due-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_execute_at_respects_injected_clock_not_wall_clock() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    executor = PlanExecutor(adapter, plan_service, audit, clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-clock-execute-at-1",
        execute_at=initial + timedelta(hours=1),
        commands=[
            Command(
                id="command-clock-execute-at-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-clock-execute-at-1",
            )
        ],
    )
    validated = plan_service.validate(plan)

    with pytest.raises(ValueError, match="not_yet_due|not yet due"):
        await executor.execute(validated)
    assert adapter.calls == []

    clock.set(initial + timedelta(hours=1, seconds=1))
    outcomes = await executor.execute(validated)

    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_no_execute_at_behaves_like_immediate_execution() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-no-execute-at-1",
        commands=[
            Command(
                id="command-no-execute-at-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-no-execute-at-1",
            )
        ],
    )

    validated = plan_service.validate(plan)
    outcomes = await executor.execute(validated)

    assert outcomes.outcomes[0].status.value == "confirmed_success"
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_expired_plan_is_stale_before_adapter_call() -> None:
    adapter, registry, _, _, plan_service, executor = await build_plan_context()
    device_id = next(device.id for device in registry.devices if device.type.value == "switch")
    plan = Plan(
        id="plan-expired-1",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        commands=[
            Command(
                id="command-expired-1",
                device_id=device_id,
                command="turn_on",
                idempotency_key="intent-expired-1",
            )
        ],
    )

    validated = plan_service.validate(plan)

    with pytest.raises(ValueError, match="expired"):
        await executor.execute(validated)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_expired_approval_makes_zero_adapter_calls_through_executor() -> None:
    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    state_store = StateStore(clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    executor = PlanExecutor(adapter, plan_service, audit, clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "cover")
    plan = Plan(
        id="plan-expired-approval-executor-1",
        commands=[
            Command(
                id="command-expired-approval-executor-1",
                device_id=device_id,
                command="open",
                risk_class=RiskClass.CONFIRM,
                idempotency_key="intent-expired-approval-executor-1",
            )
        ],
    )
    validated = plan_service.validate(plan)
    principal = OperatorPrincipal("human-1", "oidc:mfa", "session-1")
    grant = ApprovalStore(clock=clock).issue_authenticated(
        validated,
        principal=principal,
        assertion=ApprovalAssertion(
            principal=principal,
            plan_id=validated.id,
            validation_digest=validated.validation.digest if validated.validation else None,
            nonce="expired-approval-executor-1",
            approved_at=initial,
            expires_at=initial + timedelta(minutes=1),
        ),
    )
    approved = plan_service.approve(validated, grant=grant)
    clock.set(initial + timedelta(minutes=1, seconds=1))

    with pytest.raises(DomainError) as excinfo:
        await executor.execute(approved)

    assert excinfo.value.code is ErrorCode.APPROVAL_ASSERTION_EXPIRED
    assert adapter.calls == []


async def _build_context_with_failing_adapter(
    tmp_path, *, fail: dict[str, BaseException]
) -> tuple[PlanService, PlanExecutor, PlanRepository, Plan]:
    discovery_adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    state_store = StateStore()
    audit = AuditLog()
    await DiscoveryService(discovery_adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    plan_repository = PlanRepository(database)
    failing_adapter = FailureInjectingAdapter(fail=fail)
    executor = PlanExecutor(failing_adapter, plan_service, audit, plan_repository=plan_repository)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")
    plan = Plan(
        id="plan-failure-injection-1",
        commands=[
            Command(
                id="command-failure-injection-1",
                device_id=device_id,
                command="set_brightness",
                value=60,
                unit="%",
                idempotency_key="intent-failure-injection-1",
            )
        ],
    )
    return plan_service, executor, plan_repository, plan


@pytest.mark.asyncio
async def test_os_error_during_execute_does_not_brick_the_plan(tmp_path) -> None:
    plan_service, executor, plan_repository, plan = await _build_context_with_failing_adapter(
        tmp_path, fail={"execute": OSError("adapter down")}
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "unavailable"
    assert summary.outcomes[0].error is not None
    assert summary.outcomes[0].error.code == "adapter_unavailable"
    persisted = await plan_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is not PlanStatus.EXECUTING


@pytest.mark.asyncio
async def test_timeout_error_during_execute_does_not_brick_the_plan(tmp_path) -> None:
    plan_service, executor, plan_repository, plan = await _build_context_with_failing_adapter(
        tmp_path, fail={"execute": TimeoutError("adapter timed out")}
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "unavailable"
    persisted = await plan_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is not PlanStatus.EXECUTING


@pytest.mark.asyncio
async def test_os_error_during_readback_does_not_brick_the_plan(tmp_path) -> None:
    plan_service, executor, plan_repository, plan = await _build_context_with_failing_adapter(
        tmp_path, fail={"read_state": OSError("readback failed")}
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "unknown"
    assert summary.outcomes[0].error is not None
    assert summary.outcomes[0].error.code == "adapter_unavailable"
    persisted = await plan_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is not PlanStatus.EXECUTING


@pytest.mark.asyncio
async def test_timeout_error_during_readback_does_not_brick_the_plan(tmp_path) -> None:
    plan_service, executor, plan_repository, plan = await _build_context_with_failing_adapter(
        tmp_path, fail={"read_state": TimeoutError("readback timed out")}
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "unknown"
    persisted = await plan_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is not PlanStatus.EXECUTING


@pytest.mark.asyncio
async def test_plan_reaches_the_same_terminal_state_as_connection_error(tmp_path) -> None:
    """Regression guardrail: OSError/TimeoutError land in the identical
    already-correct terminal state ConnectionError already produces today —
    the plan is never left silently stuck in EXECUTING with no outcome
    recorded, and a retry via the same plan_id is refused consistently,
    the same way it already is for ConnectionError."""
    plan_service, executor, plan_repository, plan = await _build_context_with_failing_adapter(
        tmp_path, fail={"execute": ConnectionError("adapter down")}
    )
    validated = plan_service.validate(plan)

    summary = await executor.execute(validated)

    assert summary.outcomes[0].status.value == "unavailable"
    persisted = await plan_repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.UNKNOWN

    with pytest.raises(ValueError, match="invalid_transition|already"):
        await executor.execute(validated)


@pytest.mark.asyncio
async def test_failure_injection_only_fails_the_configured_method() -> None:
    adapter = FailureInjectingAdapter(fail={"discover": OSError("configured failure")})

    await adapter.connect()
    health = await adapter.health()
    ack = await adapter.execute(
        Command(
            id="probe",
            device_id="device.probe",
            command="turn_on",
            idempotency_key="probe-intent",
        )
    )

    assert health.connected is True
    assert ack.accepted is True
    with pytest.raises(OSError, match="configured failure"):
        await adapter.discover()


@pytest.mark.asyncio
async def test_single_fixed_clock_drives_every_timing_decision_consistently(tmp_path) -> None:
    """SC-001: one shared clock, advanced once, observed by expiry, scheduling,
    execution timing, and staleness together."""

    adapter = SimulatedHomeAdapter()
    registry = DeviceRegistry()
    initial = datetime(2026, 8, 19, 12, tzinfo=UTC)
    clock = FixedClock(initial)
    state_store = StateStore(timedelta(minutes=5), clock=clock)
    audit = AuditLog()
    await DiscoveryService(adapter, registry, state_store, audit).refresh()
    plan_service = PlanService(registry, state_store, PolicyEngine([]), audit, clock=clock)
    executor = PlanExecutor(adapter, plan_service, audit, clock=clock)
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    scheduled_repository = ScheduledPlanRepository(database)
    scheduler = Scheduler(executor, scheduled_repository, audit, clock=clock)
    device_id = next(device.id for device in registry.devices if device.type.value == "light")

    scheduled_plan = plan_service.validate(
        Plan(
            id="sc001-scheduled-1",
            execute_at=initial + timedelta(hours=1),
            commands=[
                Command(
                    id="sc001-scheduled-1:command",
                    device_id=device_id,
                    command="set_brightness",
                    value=60,
                    unit="%",
                    idempotency_key="sc001-scheduled-1:intent",
                )
            ],
        )
    )
    await scheduler.schedule(scheduled_plan)

    immediate_plan = plan_service.create_plan(
        "sc001-immediate-1",
        [
            Command(
                id="sc001-immediate-1:command",
                device_id=device_id,
                command="set_brightness",
                value=40,
                unit="%",
                idempotency_key="sc001-immediate-1:intent",
            )
        ],
    )
    assert immediate_plan.expires_at == initial + PlanService.DEFAULT_PLAN_TTL

    # State snapshots are stamped by DiscoveryService using real wall-clock
    # time (out of spec 047's scope), so staleness here is exercised against
    # a snapshot saved directly with received_at pinned to the clock's own
    # initial time, not against discovery-created snapshots. "power" is used
    # rather than "brightness" because the scheduled plan's set_brightness
    # command triggers a post-execution readback that would otherwise
    # overwrite the "brightness" snapshot with a fresh, real-wall-clock
    # received_at right before the final staleness check.
    await state_store.save(
        StateSnapshot(
            device_id=device_id,
            capability="power",
            value=False,
            observed_at=initial,
            received_at=initial,
            status=StateStatus.CURRENT,
            source_ref=SourceRef(adapter_id="fixture", external_id=device_id),
        )
    )

    # Before advancing: scheduled plan not yet due, freshly-saved state not stale.
    assert await scheduler.run_due() == []
    stale_before = await state_store.mark_stale()
    assert stale_before == []

    clock.set(initial + timedelta(hours=1, seconds=1))

    # After advancing: scheduled plan executes, immediate plan is now expired,
    # and state older than stale_after is reported stale — all from one advance.
    results = await scheduler.run_due()
    assert results == [{"plan_id": "sc001-scheduled-1", "outcome": "executed"}]

    validated_immediate = plan_service.validate(immediate_plan)
    with pytest.raises(ValueError, match="expired|stale"):
        plan_service.assert_executable(validated_immediate)

    stale_after_advance = await state_store.mark_stale()
    assert len(stale_after_advance) >= 1
