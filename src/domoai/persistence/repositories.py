"""Small JSON repositories backed by the local SQLite database."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from domoai.domain.models import (
    AuditEvent,
    Command,
    Device,
    ExecutionOutcome,
    Plan,
    PlanStatus,
    RecurrenceRule,
    StateSnapshot,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import redact_payload

_TABLES = {"devices", "policies", "plans"}


class SQLiteJsonRepository:
    def __init__(self, database: SQLiteDatabase, table: str, *, clock: Clock | None = None) -> None:
        if table not in _TABLES:
            raise ValueError(f"Unsupported repository table: {table}")
        self.database = database
        self.table = table
        self.clock = clock or SystemClock()

    async def save(self, identifier: str, payload: dict[str, Any]) -> None:
        timestamp = self.clock.now().isoformat()
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

    async def list_all(self) -> list[dict[str, Any]]:
        cursor = self.database.connection.execute(f"SELECT payload FROM {self.table}")
        rows = cursor.fetchall()
        cursor.close()
        return [json.loads(row[0]) for row in rows]


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

    _MAX_LIST_EVENTS_LIMIT = 500

    async def list_events(
        self,
        *,
        event_type: str | None = None,
        subject_id: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        if subject_id is not None:
            clauses.append("subject_id = ?")
            params.append(subject_id)
        if since is not None:
            clauses.append("created_at > ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = min(limit, self._MAX_LIST_EVENTS_LIMIT)
        params.append(bounded_limit)

        cursor = self.database.connection.execute(
            f"""SELECT id, event_type, actor, subject_id, payload, created_at
                FROM audit_events {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?""",
            params,
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
    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self._repository = SQLiteJsonRepository(database, "plans")
        self.clock = clock or SystemClock()

    async def save(self, plan: Plan) -> None:
        timestamp = self.clock.now().isoformat()
        self._repository.database.connection.execute(
            """INSERT INTO plans (id, payload, status, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               payload=excluded.payload, status=excluded.status, updated_at=excluded.updated_at""",
            (
                plan.id,
                json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                plan.status.value,
                timestamp,
            ),
        )
        self._repository.database.connection.commit()

    async def get(self, plan_id: str) -> Plan | None:
        payload = await self._repository.get(plan_id)
        return Plan.model_validate(payload) if payload is not None else None

    async def claim_for_execution(
        self, plan: Plan, *, allowed_statuses: frozenset[PlanStatus]
    ) -> bool:
        placeholders = ",".join("?" for _ in allowed_statuses)
        timestamp = self.clock.now().isoformat()
        cursor = self._repository.database.connection.execute(
            f"""INSERT INTO plans (id, payload, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                payload=excluded.payload, status=excluded.status, updated_at=excluded.updated_at
                WHERE plans.status IN ({placeholders})""",
            (
                plan.id,
                json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                plan.status.value,
                timestamp,
                *(status.value for status in allowed_statuses),
            ),
        )
        self._repository.database.connection.commit()
        return cursor.rowcount > 0

    async def list_by_status(self, statuses: frozenset[PlanStatus]) -> list[Plan]:
        placeholders = ",".join("?" for _ in statuses)
        cursor = self._repository.database.connection.execute(
            f"SELECT payload FROM plans WHERE status IN ({placeholders})",
            tuple(status.value for status in statuses),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [Plan.model_validate(json.loads(row[0])) for row in rows]


class DeviceRepository:
    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self._repository = SQLiteJsonRepository(database, "devices", clock=clock)

    async def save(self, device: Device) -> None:
        await self._repository.save(device.id, device.model_dump(mode="json"))

    async def get(self, device_id: str) -> Device | None:
        payload = await self._repository.get(device_id)
        return Device.model_validate(payload) if payload is not None else None

    async def list_all(self) -> list[Device]:
        return [Device.model_validate(payload) for payload in await self._repository.list_all()]

    async def delete(self, device_id: str) -> None:
        self._repository.database.connection.execute(
            "DELETE FROM devices WHERE id = ?", (device_id,)
        )
        self._repository.database.connection.commit()


class StateSnapshotRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save(self, snapshot: StateSnapshot) -> None:
        self.database.connection.execute(
            """INSERT INTO state_snapshots
               (device_id, capability, payload, observed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(device_id, capability) DO UPDATE SET
               payload=excluded.payload, observed_at=excluded.observed_at""",
            (
                snapshot.device_id,
                snapshot.capability,
                json.dumps(snapshot.model_dump(mode="json"), sort_keys=True),
                snapshot.observed_at.isoformat(),
            ),
        )
        self.database.connection.commit()

    async def list_all(self) -> list[StateSnapshot]:
        cursor = self.database.connection.execute("SELECT payload FROM state_snapshots")
        rows = cursor.fetchall()
        cursor.close()
        return [StateSnapshot.model_validate(json.loads(row[0])) for row in rows]

    async def delete(self, device_id: str) -> None:
        self.database.connection.execute(
            "DELETE FROM state_snapshots WHERE device_id = ?", (device_id,)
        )
        self.database.connection.commit()


class ExecutionOutcomeRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save(self, outcome: ExecutionOutcome) -> None:
        payload = json.dumps(outcome.model_dump(mode="json"), sort_keys=True)
        self.database.connection.execute(
            """INSERT INTO execution_outcomes
               (plan_id, command_id, payload, completed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(plan_id, command_id) DO UPDATE SET
               payload=excluded.payload, completed_at=excluded.completed_at""",
            (outcome.plan_id, outcome.command_id, payload, outcome.completed_at.isoformat()),
        )
        try:
            self.database.connection.execute(
                """INSERT INTO execution_attempts
                   (plan_id, command_id, payload, completed_at)
                   VALUES (?, ?, ?, ?)""",
                (outcome.plan_id, outcome.command_id, payload, outcome.completed_at.isoformat()),
            )
        except sqlite3.Error:
            self.database.connection.rollback()
            raise
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

    async def list_attempts_for_plan(self, plan_id: str) -> list[ExecutionOutcome]:
        cursor = self.database.connection.execute(
            """SELECT payload FROM execution_attempts
               WHERE plan_id = ? ORDER BY attempt_id""",
            (plan_id,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [ExecutionOutcome.model_validate(json.loads(row[0])) for row in rows]


class ScheduledPlanRepository:
    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def schedule(self, plan: Plan) -> None:
        if plan.execute_at is None:
            raise ValueError("plan.execute_at is required to schedule a plan")
        now = self.clock.now().isoformat()
        self.database.connection.execute(
            """INSERT INTO scheduled_plans
               (plan_id, execute_at, status, payload, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (
                plan.id,
                plan.execute_at.isoformat(),
                json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                now,
            ),
        )
        self.database.connection.commit()

    async def get(self, plan_id: str) -> tuple[Plan, str] | None:
        cursor = self.database.connection.execute(
            "SELECT payload, status FROM scheduled_plans WHERE plan_id = ?",
            (plan_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return None
        return Plan.model_validate(json.loads(row[0])), row[1]

    async def list_pending(self) -> list[Plan]:
        cursor = self.database.connection.execute(
            """SELECT payload FROM scheduled_plans
               WHERE status = 'pending' ORDER BY execute_at""",
        )
        rows = cursor.fetchall()
        cursor.close()
        return [Plan.model_validate(json.loads(row[0])) for row in rows]

    async def mark_executed(self, plan_id: str) -> None:
        await self._transition(plan_id, "executed")

    async def mark_missed(self, plan_id: str) -> None:
        await self._transition(plan_id, "missed")

    async def cancel(self, plan_id: str) -> bool:
        return await self._transition(plan_id, "cancelled")

    async def reschedule(self, plan_id: str, execute_at: datetime) -> bool:
        existing = await self.get(plan_id)
        if existing is None or existing[1] != "pending":
            return False
        plan, _status = existing
        updated_plan = plan.model_copy(update={"execute_at": execute_at})
        cursor = self.database.connection.execute(
            """UPDATE scheduled_plans SET execute_at = ?, payload = ?, updated_at = ?
               WHERE plan_id = ? AND status = 'pending'""",
            (
                execute_at.isoformat(),
                json.dumps(updated_plan.model_dump(mode="json"), sort_keys=True),
                self.clock.now().isoformat(),
                plan_id,
            ),
        )
        self.database.connection.commit()
        return cursor.rowcount > 0

    async def _transition(self, plan_id: str, status: str) -> bool:
        cursor = self.database.connection.execute(
            """UPDATE scheduled_plans SET status = ?, updated_at = ?
               WHERE plan_id = ? AND status = 'pending'""",
            (status, self.clock.now().isoformat(), plan_id),
        )
        self.database.connection.commit()
        return cursor.rowcount > 0


class RecurringScheduleRepository:
    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def create(
        self,
        schedule_id: str,
        commands: list[Command],
        rule: RecurrenceRule,
        next_execute_at: datetime,
    ) -> None:
        now = self.clock.now().isoformat()
        self.database.connection.execute(
            """INSERT INTO recurring_schedules
               (schedule_id, template_payload, recurrence_payload, next_execute_at,
                status, updated_at)
               VALUES (?, ?, ?, ?, 'active', ?)""",
            (
                schedule_id,
                json.dumps(
                    [command.model_dump(mode="json") for command in commands], sort_keys=True
                ),
                json.dumps(rule.model_dump(mode="json"), sort_keys=True),
                next_execute_at.isoformat(),
                now,
            ),
        )
        self.database.connection.commit()

    async def get(
        self, schedule_id: str
    ) -> tuple[list[Command], RecurrenceRule, datetime, str] | None:
        cursor = self.database.connection.execute(
            """SELECT template_payload, recurrence_payload, next_execute_at, status
               FROM recurring_schedules WHERE schedule_id = ?""",
            (schedule_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return None
        commands = [Command.model_validate(item) for item in json.loads(row[0])]
        rule = RecurrenceRule.model_validate(json.loads(row[1]))
        return commands, rule, datetime.fromisoformat(row[2]), row[3]

    async def list_active(
        self,
    ) -> list[tuple[str, list[Command], RecurrenceRule, datetime]]:
        cursor = self.database.connection.execute(
            """SELECT schedule_id, template_payload, recurrence_payload, next_execute_at
               FROM recurring_schedules WHERE status = 'active' ORDER BY next_execute_at""",
        )
        rows = cursor.fetchall()
        cursor.close()
        return [
            (
                row[0],
                [Command.model_validate(item) for item in json.loads(row[1])],
                RecurrenceRule.model_validate(json.loads(row[2])),
                datetime.fromisoformat(row[3]),
            )
            for row in rows
        ]

    async def advance(self, schedule_id: str, next_execute_at: datetime) -> None:
        self.database.connection.execute(
            """UPDATE recurring_schedules SET next_execute_at = ?, updated_at = ?
               WHERE schedule_id = ? AND status = 'active'""",
            (next_execute_at.isoformat(), self.clock.now().isoformat(), schedule_id),
        )
        self.database.connection.commit()

    async def cancel(self, schedule_id: str) -> bool:
        cursor = self.database.connection.execute(
            """UPDATE recurring_schedules SET status = 'cancelled', updated_at = ?
               WHERE schedule_id = ? AND status = 'active'""",
            (self.clock.now().isoformat(), schedule_id),
        )
        self.database.connection.commit()
        return cursor.rowcount > 0
