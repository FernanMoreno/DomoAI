from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.discovery_service import DiscoveryService
from domoai.application.plan_service import PlanService
from domoai.domain.models import Command, Plan, PlanStatus, Precondition, RiskClass
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import ApprovalStore
from domoai.runtime.events import AuditLog
from domoai.runtime.executor import PlanExecutor
from domoai.runtime.policy_engine import PolicyEngine
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore


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

    grant = ApprovalStore().issue(validated, approved_by="local_operator")
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
