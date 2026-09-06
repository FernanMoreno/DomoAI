import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domoai.domain.models import (
    AuditEvent,
    AuthorityContext,
    Command,
    Plan,
    PlanStatus,
    PrincipalRole,
)
from domoai.persistence.repositories import AuditEventRepository, PlanRepository
from domoai.persistence.sqlite import SQLiteDatabase


def _plan(*, plan_id: str, authority: AuthorityContext | None = None) -> Plan:
    return Plan(
        id=plan_id,
        authority=authority or AuthorityContext(),
        commands=[
            Command(
                id=f"command-{plan_id}",
                device_id="light.one",
                command="turn_on",
                idempotency_key=f"intent-{plan_id}",
            )
        ],
    )


@pytest.mark.asyncio
async def test_plan_and_audit_authority_round_trip_through_separate_lanes(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.sqlite3")
    audit_database = SQLiteDatabase(tmp_path / "audit.sqlite3")
    await database.initialize()
    await audit_database.initialize()
    try:
        authority = AuthorityContext(
            tenant_id="tenant-a",
            household_id="home-a",
            household_ids=["home-a", "home-b"],
            principal_id="operator-a",
            roles=[PrincipalRole.OPERATOR],
            device_ids=["light.one"],
        )
        plan_repository = PlanRepository(database)
        await plan_repository.save(_plan(plan_id="bound-plan", authority=authority))
        restored_plan = await plan_repository.get("bound-plan")

        assert restored_plan is not None
        assert restored_plan.authority == authority

        event = AuditEvent(
            id="identity-audit-1",
            event_type="plan_execution_started",
            actor="operator-a",
            subject_id="bound-plan",
            payload={"safe": True},
            created_at=datetime(2026, 9, 4, 10, tzinfo=UTC),
            authority=authority,
        )
        audit_repository = AuditEventRepository(audit_database)
        audit_repository.append_event(event)
        restored_events = await audit_repository.list_all()
        restored_outbox = await audit_repository.list_pending_outbox()

        assert restored_events[0].authority == authority
        assert restored_outbox[0].authority == authority
    finally:
        await database.close()
        await audit_database.close()


@pytest.mark.asyncio
async def test_legacy_plan_payload_loads_with_default_local_authority(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "legacy.sqlite3")
    await database.initialize()
    try:
        legacy = _plan(plan_id="legacy-plan")
        payload = legacy.model_dump(mode="json")
        payload.pop("authority")
        database.connection.execute(
            "INSERT INTO plans (id, payload, status, updated_at) VALUES (?, ?, ?, ?)",
            (
                legacy.id,
                json.dumps(payload, sort_keys=True),
                PlanStatus.DRAFT.value,
                "2026-09-04T10:00:00+00:00",
            ),
        )
        database.connection.commit()

        restored = await PlanRepository(database).get(legacy.id)

        assert restored is not None
        assert restored.authority == AuthorityContext()
    finally:
        await database.close()
