"""Small JSON repositories backed by the local SQLite database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from domoai.domain.models import AuditEvent, ExecutionOutcome, Plan
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.events import redact_payload

_TABLES = {"devices", "policies", "plans"}


class SQLiteJsonRepository:
    def __init__(self, database: SQLiteDatabase, table: str) -> None:
        if table not in _TABLES:
            raise ValueError(f"Unsupported repository table: {table}")
        self.database = database
        self.table = table

    async def save(self, identifier: str, payload: dict[str, Any]) -> None:
        timestamp = datetime.now(UTC).isoformat()
        self.database.connection.execute(
            f"""INSERT INTO {self.table} (id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload, updated_at=excluded.updated_at""",
            (identifier, json.dumps(payload, sort_keys=True), timestamp),
        )
        self.database.connection.commit()

    async def get(self, identifier: str) -> dict[str, Any] | None:
        cursor = self.database.connection.execute(
            f"SELECT payload FROM {self.table} WHERE id = ?", (identifier,)
        )
        row = cursor.fetchone()
        cursor.close()
        return json.loads(row[0]) if row else None


class AuditEventRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def append(
        self,
        *,
        event_id: str,
        event_type: str,
        actor: str,
        subject_id: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> None:
        self.append_event(
            AuditEvent(
                id=event_id,
                event_type=event_type,
                actor=actor,
                subject_id=subject_id,
                payload=payload,
                created_at=datetime.fromisoformat(created_at),
            )
        )

    def append_event(self, event: AuditEvent) -> None:
        self.database.connection.execute(
            """INSERT INTO audit_events
               (id, event_type, actor, subject_id, payload, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.event_type,
                event.actor,
                event.subject_id,
                json.dumps(redact_payload(event.payload), sort_keys=True),
                event.created_at.isoformat(),
            ),
        )
        self.database.connection.commit()

    async def list_all(self) -> list[AuditEvent]:
        cursor = self.database.connection.execute(
            """SELECT id, event_type, actor, subject_id, payload, created_at
               FROM audit_events ORDER BY created_at, id"""
        )
        rows = cursor.fetchall()
        cursor.close()
        return [
            AuditEvent(
                id=row[0],
                event_type=row[1],
                actor=row[2],
                subject_id=row[3],
                payload=json.loads(row[4]),
                created_at=row[5],
            )
            for row in rows
        ]


class PlanRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self._repository = SQLiteJsonRepository(database, "plans")

    async def save(self, plan: Plan) -> None:
        await self._repository.save(plan.id, plan.model_dump(mode="json"))

    async def get(self, plan_id: str) -> Plan | None:
        payload = await self._repository.get(plan_id)
        return Plan.model_validate(payload) if payload is not None else None


class ExecutionOutcomeRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save(self, outcome: ExecutionOutcome) -> None:
        self.database.connection.execute(
            """INSERT INTO execution_outcomes
               (plan_id, command_id, payload, completed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(plan_id, command_id) DO UPDATE SET
               payload=excluded.payload, completed_at=excluded.completed_at""",
            (
                outcome.plan_id,
                outcome.command_id,
                json.dumps(outcome.model_dump(mode="json"), sort_keys=True),
                outcome.completed_at.isoformat(),
            ),
        )
        self.database.connection.commit()

    async def list_for_plan(self, plan_id: str) -> list[ExecutionOutcome]:
        cursor = self.database.connection.execute(
            """SELECT payload FROM execution_outcomes
               WHERE plan_id = ? ORDER BY completed_at, command_id""",
            (plan_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [ExecutionOutcome.model_validate(json.loads(row[0])) for row in rows]
