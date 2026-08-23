from __future__ import annotations

import pytest

from domoai.domain.errors import DomainError, ErrorCode, InvalidTransitionError
from domoai.domain.models import Command, Plan, PlanStatus
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
