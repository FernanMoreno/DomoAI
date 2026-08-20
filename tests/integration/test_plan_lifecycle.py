import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import (
    Command,
    Plan,
    PlanStatus,
    Precondition,
    RiskClass,
    SourceRef,
    StateSnapshot,
    StateStatus,
)
from domoai.persistence.repositories import PlanRepository, ScheduledPlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.clock import FixedClock
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.scheduler import Scheduler
from domoai.runtime.state_store import StateStore
from tests.fixtures.failure_injection import FailureInjectingAdapter


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


async def build_plan_context_with_repository(tmp_path) -> tuple[
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

    grant = ApprovalStore(operator_token="test-operator-secret").issue(
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
async def test_double_execution_of_terminal_plan_is_refused(tmp_path) -> None:
    adapter, registry, _, _, plan_service, executor, _ = (
        await build_plan_context_with_repository(tmp_path)
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
    adapter, registry, _, _, plan_service, executor, plan_repository = (
        await build_plan_context_with_repository(tmp_path)
    )
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
async def test_first_time_execution_with_repository_is_unaffected(tmp_path) -> None:
    adapter, registry, _, _, plan_service, executor, _ = (
        await build_plan_context_with_repository(tmp_path)
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

    async def claim_for_execution(
        self, plan: Plan, *, allowed_statuses: frozenset[PlanStatus]
    ) -> bool:
        await asyncio.sleep(0)
        return await self._inner.claim_for_execution(plan, allowed_statuses=allowed_statuses)


@pytest.mark.asyncio
async def test_two_concurrent_execution_attempts_on_the_same_plan_have_exactly_one_winner(
    tmp_path,
) -> None:
    adapter, registry, _, _, plan_service, executor, plan_repository = (
        await build_plan_context_with_repository(tmp_path)
    )
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
    executor = PlanExecutor(
        failing_adapter, plan_service, audit, plan_repository=plan_repository
    )
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
