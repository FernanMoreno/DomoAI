from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from domoai.domain.errors import DomainError, ErrorCode, InvalidTransitionError
from domoai.domain.models import Command, Plan, PlanExecutionLease, PlanStatus
from domoai.persistence.repositories import PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _plan(plan_id: str, status: PlanStatus) -> Plan:
    return Plan(
        id=plan_id,
        status=status,
        commands=[
            Command(
                id=f"{plan_id}:command",
                device_id="garden.garden-pump",
                command="turn_on",
                idempotency_key=f"{plan_id}:intent",
            )
        ],
    )


def _lease(deployment_id: str, *, epoch: int, expires_at: datetime) -> PlanExecutionLease:
    return PlanExecutionLease(
        deployment_id=deployment_id,
        owner_id=f"owner-{deployment_id}",
        lease_id=f"lease-{deployment_id}-{epoch}",
        fencing_epoch=epoch,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_list_by_status_returns_only_matching_plans(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    executing = _plan("plan-executing", PlanStatus.EXECUTING)
    ready = _plan("plan-ready", PlanStatus.READY)
    await repository.save(executing)
    await repository.save(ready)

    result = await repository.list_by_status(frozenset({PlanStatus.EXECUTING}))

    assert [plan.id for plan in result] == ["plan-executing"]


@pytest.mark.asyncio
async def test_list_by_status_supports_multiple_statuses(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    executing = _plan("plan-executing", PlanStatus.EXECUTING)
    failed = _plan("plan-failed", PlanStatus.FAILED)
    ready = _plan("plan-ready", PlanStatus.READY)
    await repository.save(executing)
    await repository.save(failed)
    await repository.save(ready)

    result = await repository.list_by_status(frozenset({PlanStatus.EXECUTING, PlanStatus.FAILED}))

    assert {plan.id for plan in result} == {"plan-executing", "plan-failed"}


@pytest.mark.asyncio
async def test_list_by_status_on_empty_store_returns_empty_list(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)

    result = await repository.list_by_status(frozenset({PlanStatus.EXECUTING}))

    assert result == []


@pytest.mark.asyncio
async def test_claim_for_execution_rejects_non_executable_statuses_at_repository_boundary(
    tmp_path,
) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    plan = _plan("plan-draft", PlanStatus.DRAFT)
    await repository.save(plan)

    claimed = await repository.claim_for_execution(
        plan.model_copy(update={"status": PlanStatus.EXECUTING}),
        allowed_statuses=frozenset({PlanStatus.DRAFT, PlanStatus.READY}),
    )

    assert claimed is False
    persisted = await repository.get(plan.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.DRAFT


@pytest.mark.asyncio
async def test_save_rejects_reopening_terminal_plan(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    terminal = _plan("plan-terminal-immutable", PlanStatus.COMPLETED)
    await repository.save(terminal)

    with pytest.raises(InvalidTransitionError):
        await repository.save(terminal.model_copy(update={"status": PlanStatus.READY}))

    persisted = await repository.get(terminal.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_save_rejects_changed_definition_for_existing_plan_id(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    original = _plan("plan-definition-immutable", PlanStatus.READY).model_copy(
        update={"definition_digest": "sha256:original"}
    )
    await repository.save(original)

    changed = original.model_copy(update={"definition_digest": "sha256:changed"})
    with pytest.raises(DomainError) as error:
        await repository.save(changed)

    assert error.value.code is ErrorCode.PLAN_IDENTITY_CONFLICT
    persisted = await repository.get(original.id)
    assert persisted is not None
    assert persisted.definition_digest == "sha256:original"


@pytest.mark.asyncio
async def test_save_validation_rejects_terminal_plan_before_rewriting_it(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    terminal = _plan("plan-validation-terminal", PlanStatus.COMPLETED)
    await repository.save(terminal)

    with pytest.raises(InvalidTransitionError):
        await repository.save_validation(terminal.model_copy(update={"status": PlanStatus.READY}))

    persisted = await repository.get(terminal.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_claim_rejects_definition_mismatch_for_existing_plan_id(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    original = _plan("plan-claim-definition-immutable", PlanStatus.READY).model_copy(
        update={"definition_digest": "sha256:original"}
    )
    await repository.save(original)

    changed = original.model_copy(
        update={
            "definition_digest": "sha256:changed",
            "status": PlanStatus.EXECUTING,
        }
    )
    with pytest.raises(DomainError) as error:
        await repository.claim_for_execution(
            changed,
            allowed_statuses=frozenset({PlanStatus.READY}),
        )

    assert error.value.code is ErrorCode.PLAN_IDENTITY_CONFLICT
    persisted = await repository.get(original.id)
    assert persisted is not None
    assert persisted.status is PlanStatus.READY
    assert persisted.definition_digest == "sha256:original"


@pytest.mark.asyncio
async def test_lifecycle_specific_approval_and_settlement_guards(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)

    ready = _plan("plan-lifecycle-specific", PlanStatus.READY)
    with pytest.raises(InvalidTransitionError):
        await repository.save_approval(ready)

    requires_confirmation = ready.model_copy(update={"status": PlanStatus.REQUIRES_CONFIRMATION})
    await repository.save(requires_confirmation)
    approved = requires_confirmation.model_copy(update={"status": PlanStatus.APPROVED})
    await repository.save_approval(approved)
    assert (await repository.get(approved.id)).status is PlanStatus.APPROVED

    with pytest.raises(InvalidTransitionError):
        await repository.settle_execution(approved)

    executing = approved.model_copy(update={"status": PlanStatus.EXECUTING})
    await repository.save(executing)
    completed = executing.model_copy(update={"status": PlanStatus.COMPLETED})
    await repository.settle_execution(completed)
    assert (await repository.get(completed.id)).status is PlanStatus.COMPLETED


@pytest.mark.asyncio
async def test_repository_binds_plan_to_configured_deployment(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database, deployment_id="edge-a")

    plan = _plan("plan-bound", PlanStatus.DRAFT)
    await repository.save(plan)

    persisted = await repository.get(plan.id)
    assert persisted is not None
    assert persisted.deployment_id == "edge-a"

    with pytest.raises(DomainError) as error:
        await repository.save(
            Plan.model_validate(
                plan.model_dump(exclude={"schema_version"}) | {"deployment_id": "edge-b"}
            )
        )

    assert error.value.code is ErrorCode.PLAN_IDENTITY_CONFLICT


@pytest.mark.asyncio
async def test_list_by_status_filters_plans_by_deployment(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    await repository.save(
        Plan.model_validate(
            _plan("plan-a", PlanStatus.EXECUTING).model_dump(exclude={"schema_version"})
            | {"deployment_id": "edge-a"}
        )
    )
    await repository.save(
        Plan.model_validate(
            _plan("plan-b", PlanStatus.EXECUTING).model_dump(exclude={"schema_version"})
            | {"deployment_id": "edge-b"}
        )
    )

    result = await repository.list_by_status(
        frozenset({PlanStatus.EXECUTING}), deployment_id="edge-a"
    )

    assert [plan.id for plan in result] == ["plan-a"]


@pytest.mark.asyncio
async def test_mark_unknown_requires_the_expected_execution_lease(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "repo.sqlite3")
    await database.initialize()
    repository = PlanRepository(database)
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    lease = _lease("edge-a", epoch=1, expires_at=expires_at)
    plan = Plan.model_validate(
        _plan("plan-lease-cas", PlanStatus.EXECUTING).model_dump(exclude={"schema_version"})
        | {"deployment_id": "edge-a", "execution_lease": lease}
    )
    await repository.save(plan)

    stale = lease.model_copy(update={"lease_id": "different-lease"})
    assert (
        await repository.mark_unknown_if_executing(
            plan.id, expected_execution_lease=stale
        )
    ) is False
    persisted = await repository.get(plan.id)
    assert persisted is not None and persisted.status is PlanStatus.EXECUTING

    assert (
        await repository.mark_unknown_if_executing(
            plan.id, expected_execution_lease=lease
        )
    ) is True
