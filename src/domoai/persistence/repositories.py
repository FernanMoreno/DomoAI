"""Small JSON repositories backed by the local SQLite database."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from domoai.domain.errors import DomainError, ErrorCode, InvalidTransitionError
from domoai.domain.models import (
    AuditEvent,
    BundleCommit,
    BundleCommitStatus,
    BundleMemberCommit,
    BundleMemberCommitStatus,
    Command,
    Device,
    ExecutionOutcome,
    ExecutionStatus,
    ExecutionWindow,
    Plan,
    PlanStatus,
    RecurrenceRule,
    StateSnapshot,
)
from domoai.domain.transitions import assert_plan_transition
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import redact_payload
from domoai.runtime.state_store import StateStoreMetadata

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
               FROM audit_events ORDER BY julianday(created_at), id"""
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
            if since.tzinfo is None or since.utcoffset() is None:
                raise ValueError("since must be timezone-aware")
            clauses.append("julianday(created_at) > julianday(?)")
            params.append(since.astimezone(UTC).isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bounded_limit = min(limit, self._MAX_LIST_EVENTS_LIMIT)
        params.append(bounded_limit)

        cursor = self.database.connection.execute(
            f"""SELECT id, event_type, actor, subject_id, payload, created_at
                FROM audit_events {where}
                ORDER BY julianday(created_at) DESC, id DESC
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

    async def save_validation(self, plan: Plan) -> None:
        """Persist validation evidence without bypassing lifecycle guards."""

        await self.save(plan)

    async def save_approval(self, plan: Plan) -> None:
        """Persist an approval transition without accepting arbitrary states."""

        if plan.status is not PlanStatus.APPROVED:
            raise InvalidTransitionError(plan.status.value, PlanStatus.APPROVED.value)
        await self.save(plan)

    async def settle_execution(self, plan: Plan) -> None:
        """Persist terminal execution evidence without reopening the plan."""

        terminal_statuses = {
            PlanStatus.COMPLETED,
            PlanStatus.PARTIALLY_FAILED,
            PlanStatus.FAILED,
            PlanStatus.UNKNOWN,
            PlanStatus.CANCELLED,
        }
        if plan.status not in terminal_statuses:
            raise InvalidTransitionError(plan.status.value, "terminal")
        persisted = await self.get(plan.id)
        if persisted is None or persisted.status is not PlanStatus.EXECUTING:
            current = persisted.status.value if persisted is not None else "missing"
            raise InvalidTransitionError(current, plan.status.value)
        await self.save(plan)

    async def save(self, plan: Plan) -> None:
        connection = self._repository.database.connection
        try:
            # Lifecycle evidence and identity checks must observe the same
            # snapshot as the write. This serializes competing writers before
            # either can replace the definition bound to a plan id.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status, payload FROM plans WHERE id = ?",
                (plan.id,),
            ).fetchone()
            if existing is not None:
                current_status = PlanStatus(existing[0])
                if current_status is not plan.status:
                    assert_plan_transition(current_status, plan.status)
                stored_plan = Plan.model_validate(json.loads(existing[1]))
                if (
                    stored_plan.definition_digest is not None
                    and stored_plan.definition_digest != plan.definition_digest
                ):
                    raise DomainError(
                        ErrorCode.PLAN_IDENTITY_CONFLICT,
                        "Plan identity is already bound to a different definition",
                        details={"plan_id": plan.id},
                    )
            timestamp = self.clock.now().isoformat()
            connection.execute(
                """INSERT INTO plans (id, payload, status, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                   payload=excluded.payload,
                   status=excluded.status,
                   updated_at=excluded.updated_at""",
                (
                    plan.id,
                    json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                    plan.status.value,
                    timestamp,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def get(self, plan_id: str) -> Plan | None:
        payload = await self._repository.get(plan_id)
        return Plan.model_validate(payload) if payload is not None else None

    async def mark_unknown_if_executing(self, plan_id: str) -> bool:
        """Atomically settle orphan evidence without making it claimable again."""

        row = self._repository.database.connection.execute(
            "SELECT payload FROM plans WHERE id = ? AND status = ?",
            (plan_id, PlanStatus.EXECUTING.value),
        ).fetchone()
        if row is None:
            return False
        payload = json.loads(row[0])
        payload["status"] = PlanStatus.UNKNOWN.value
        cursor = self._repository.database.connection.execute(
            """UPDATE plans SET status = ?, payload = ?, updated_at = ?
               WHERE id = ? AND status = ?""",
            (
                PlanStatus.UNKNOWN.value,
                json.dumps(payload, sort_keys=True),
                self.clock.now().isoformat(),
                plan_id,
                PlanStatus.EXECUTING.value,
            ),
        )
        if cursor.rowcount == 0:
            self._repository.database.connection.rollback()
            return False
        self._repository.database.connection.commit()
        return True

    async def claim_for_execution(
        self, plan: Plan, *, allowed_statuses: frozenset[PlanStatus]
    ) -> bool:
        if plan.status is not PlanStatus.EXECUTING:
            return False
        claimable_statuses = frozenset(
            status
            for status in allowed_statuses
            if status in {PlanStatus.READY, PlanStatus.APPROVED}
        )
        if not claimable_statuses:
            return False
        placeholders = ",".join("?" for _ in claimable_statuses)
        timestamp = self.clock.now().isoformat()
        connection = self._repository.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status, payload FROM plans WHERE id = ?",
                (plan.id,),
            ).fetchone()
            if existing is not None:
                stored_plan = Plan.model_validate(json.loads(existing[1]))
                if (
                    stored_plan.definition_digest is not None
                    and stored_plan.definition_digest != plan.definition_digest
                ):
                    raise DomainError(
                        ErrorCode.PLAN_IDENTITY_CONFLICT,
                        "Plan identity is already bound to a different definition",
                        details={"plan_id": plan.id},
                    )
                cursor = connection.execute(
                    f"""UPDATE plans
                        SET payload = ?, status = ?, updated_at = ?
                        WHERE id = ? AND status IN ({placeholders})""",
                    (
                        json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                        plan.status.value,
                        timestamp,
                        plan.id,
                        *(status.value for status in claimable_statuses),
                    ),
                )
            else:
                cursor = connection.execute(
                    """INSERT INTO plans (id, payload, status, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        plan.id,
                        json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                        plan.status.value,
                        timestamp,
                    ),
                )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            connection.rollback()
            raise

    async def list_by_status(self, statuses: frozenset[PlanStatus]) -> list[Plan]:
        placeholders = ",".join("?" for _ in statuses)
        cursor = self._repository.database.connection.execute(
            f"SELECT payload FROM plans WHERE status IN ({placeholders})",
            tuple(status.value for status in statuses),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [Plan.model_validate(json.loads(row[0])) for row in rows]


class BundleCommitRepository:
    """Durable aggregate state for one ordered bundle commit saga."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def save(self, bundle: BundleCommit) -> BundleCommit:
        updated = bundle.model_copy(update={"updated_at": self.clock.now()})
        self.database.connection.execute(
            """INSERT INTO bundle_commits
               (id, bundle_digest, status, payload, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               bundle_digest=excluded.bundle_digest,
               status=excluded.status,
               payload=excluded.payload,
               updated_at=excluded.updated_at""",
            (
                updated.id,
                updated.bundle_digest,
                updated.status.value,
                json.dumps(updated.model_dump(mode="json"), sort_keys=True),
                updated.updated_at.isoformat(),
            ),
        )
        self.database.connection.commit()
        return updated

    async def get(self, bundle_id: str) -> BundleCommit | None:
        cursor = self.database.connection.execute(
            "SELECT payload FROM bundle_commits WHERE id = ?", (bundle_id,)
        )
        row = cursor.fetchone()
        cursor.close()
        return BundleCommit.model_validate(json.loads(row[0])) if row else None

    async def get_by_digest(self, bundle_digest: str) -> BundleCommit | None:
        cursor = self.database.connection.execute(
            "SELECT payload FROM bundle_commits WHERE bundle_digest = ?", (bundle_digest,)
        )
        row = cursor.fetchone()
        cursor.close()
        return BundleCommit.model_validate(json.loads(row[0])) if row else None

    async def get_for_plan(self, plan_id: str) -> BundleCommit | None:
        cursor = self.database.connection.execute("SELECT payload FROM bundle_commits")
        rows = cursor.fetchall()
        cursor.close()
        for row in rows:
            bundle = BundleCommit.model_validate(json.loads(row[0]))
            if any(member.plan_id == plan_id for member in bundle.members):
                return bundle
        return None

    async def record_member_outcome(
        self,
        plan_id: str,
        *,
        status: BundleMemberCommitStatus,
        execution_status: ExecutionStatus | None,
        details: dict[str, Any],
    ) -> BundleCommit | None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT id, payload FROM bundle_commits").fetchall()
            stored: BundleCommit | None = None
            for row in rows:
                candidate = BundleCommit.model_validate(json.loads(row[1]))
                if any(member.plan_id == plan_id for member in candidate.members):
                    stored = candidate
                    break
            if stored is None:
                connection.commit()
                return None
            index = next(
                index for index, member in enumerate(stored.members) if member.plan_id == plan_id
            )
            current = stored.members[index]
            if current.status in {
                BundleMemberCommitStatus.EXECUTED,
                BundleMemberCommitStatus.FAILED,
                BundleMemberCommitStatus.UNKNOWN,
                BundleMemberCommitStatus.MISSED,
            }:
                # Re-delivery is safe only when it carries the same terminal
                # evidence. A conflicting second settlement is not allowed to
                # rewrite the fulfillment ledger.
                if current.status is status and current.execution_status is execution_status:
                    connection.commit()
                    return stored
                connection.rollback()
                raise ValueError(f"bundle member {plan_id} is already terminal")
            members = list(stored.members)
            members[index] = current.model_copy(
                update={
                    "status": status,
                    "execution_status": execution_status,
                    "details": details,
                    "scheduled": True,
                }
            )
            aggregate_status = self._aggregate_fulfillment_status(members)
            updated = stored.model_copy(
                update={
                    "members": members,
                    "status": aggregate_status,
                    "updated_at": self.clock.now(),
                }
            )
            cursor = connection.execute(
                """UPDATE bundle_commits
                   SET status = ?, payload = ?, updated_at = ?
                   WHERE id = ? AND status IN (?, ?, ?)""",
                (
                    updated.status.value,
                    json.dumps(updated.model_dump(mode="json"), sort_keys=True),
                    updated.updated_at.isoformat(),
                    updated.id,
                    BundleCommitStatus.SCHEDULED.value,
                    BundleCommitStatus.PARTIALLY_COMMITTED.value,
                    BundleCommitStatus.COMMITTING.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"bundle {updated.id} fulfillment CAS failed")
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _aggregate_fulfillment_status(
        members: Sequence[BundleMemberCommit],
    ) -> BundleCommitStatus:
        statuses = {member.status for member in members}
        if statuses == {BundleMemberCommitStatus.EXECUTED}:
            return BundleCommitStatus.COMPLETED
        if BundleMemberCommitStatus.PENDING in statuses:
            return BundleCommitStatus.COMMITTING
        if statuses & {BundleMemberCommitStatus.SCHEDULED}:
            return BundleCommitStatus.SCHEDULED
        if BundleMemberCommitStatus.UNKNOWN in statuses:
            return (
                BundleCommitStatus.PARTIALLY_COMMITTED
                if BundleMemberCommitStatus.EXECUTED in statuses
                else BundleCommitStatus.UNKNOWN
            )
        if BundleMemberCommitStatus.MISSED in statuses:
            return (
                BundleCommitStatus.MISSED
                if statuses == {BundleMemberCommitStatus.MISSED}
                else BundleCommitStatus.PARTIALLY_COMMITTED
            )
        if BundleMemberCommitStatus.FAILED in statuses:
            return (
                BundleCommitStatus.PARTIALLY_COMMITTED
                if BundleMemberCommitStatus.EXECUTED in statuses
                else BundleCommitStatus.FAILED
            )
        return BundleCommitStatus.UNKNOWN

    async def list_non_terminal(self) -> list[BundleCommit]:
        placeholders = ",".join("?" for _ in ("committing", "scheduled", "partial"))
        cursor = self.database.connection.execute(
            (
                "SELECT payload FROM bundle_commits "
                f"WHERE status IN ({placeholders}) ORDER BY updated_at"
            ),
            (
                BundleCommitStatus.COMMITTING.value,
                BundleCommitStatus.SCHEDULED.value,
                BundleCommitStatus.PARTIALLY_COMMITTED.value,
            ),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [BundleCommit.model_validate(json.loads(row[0])) for row in rows]

    async def is_scheduled_member(self, plan_id: str) -> bool:
        cursor = self.database.connection.execute(
            "SELECT payload FROM bundle_commits WHERE status = ?",
            (BundleCommitStatus.SCHEDULED.value,),
        )
        rows = cursor.fetchall()
        cursor.close()
        for row in rows:
            bundle = BundleCommit.model_validate(json.loads(row[0]))
            if any(
                member.plan_id == plan_id
                and member.status is BundleMemberCommitStatus.SCHEDULED
                for member in bundle.members
            ):
                return True
        return False

    async def schedule_members_transaction(
        self,
        bundle: BundleCommit,
        plans: list[Plan],
        member_indexes: list[int],
        *,
        final_status: BundleCommitStatus,
    ) -> BundleCommit:
        if len(plans) != len(member_indexes):
            raise ValueError("plans and member_indexes must have the same length")
        connection = self.database.connection
        try:
            connection.execute("BEGIN")
            for plan in plans:
                if plan.execute_at is None:
                    raise ValueError("future bundle members require execute_at")
                connection.execute(
                    """INSERT INTO scheduled_plans
                       (plan_id, execute_at, status, payload, updated_at)
                       VALUES (?, ?, 'pending', ?, ?)""",
                    (
                        plan.id,
                        plan.execute_at.isoformat(),
                        json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                        self.clock.now().isoformat(),
                    ),
                )
            members = list(bundle.members)
            for index in member_indexes:
                members[index] = members[index].model_copy(
                    update={
                        "status": BundleMemberCommitStatus.SCHEDULED,
                        "scheduled": True,
                    }
                )
            updated = bundle.model_copy(
                update={
                    "members": members,
                    "status": final_status,
                    "updated_at": self.clock.now(),
                }
            )
            connection.execute(
                """UPDATE bundle_commits
                   SET status = ?, payload = ?, updated_at = ?
                   WHERE id = ?""",
                (
                    updated.status.value,
                    json.dumps(updated.model_dump(mode="json"), sort_keys=True),
                    updated.updated_at.isoformat(),
                    updated.id,
                ),
            )
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise


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


class RuntimeStateMetadataRepository:
    """Persists StateStore's revision/version continuity across restarts."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def get(self) -> StateStoreMetadata | None:
        cursor = self.database.connection.execute(
            "SELECT payload FROM runtime_state_metadata WHERE id = 1"
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        state_versions: dict[tuple[str, str], int] = {}
        raw_state_versions = payload.get("state_versions", {})
        if not isinstance(raw_state_versions, dict):
            raw_state_versions = {}
        for key, version in raw_state_versions.items():
            device_id, separator, capability = str(key).partition("::")
            if (
                separator
                and device_id
                and capability
                and isinstance(version, int)
                and not isinstance(version, bool)
            ):
                state_versions[(device_id, capability)] = version
        inventory_revision = payload.get("inventory_revision", 0)
        version_counter = payload.get("version_counter", 0)
        if not isinstance(inventory_revision, int) or isinstance(inventory_revision, bool):
            inventory_revision = 0
        if not isinstance(version_counter, int) or isinstance(version_counter, bool):
            version_counter = 0
        inventory_fingerprint = payload.get("inventory_fingerprint")
        if not isinstance(inventory_fingerprint, str) or not inventory_fingerprint:
            inventory_fingerprint = None
        return StateStoreMetadata(
            inventory_revision=max(0, inventory_revision),
            version_counter=max(0, version_counter),
            state_versions=state_versions,
            inventory_fingerprint=inventory_fingerprint,
        )

    async def save(self, metadata: StateStoreMetadata) -> None:
        payload = {
            "inventory_revision": metadata.inventory_revision,
            "version_counter": metadata.version_counter,
            "state_versions": {
                f"{device_id}::{capability}": version
                for (device_id, capability), version in metadata.state_versions.items()
            },
            "inventory_fingerprint": metadata.inventory_fingerprint,
        }
        self.database.connection.execute(
            """INSERT INTO runtime_state_metadata (id, payload, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               payload=excluded.payload, updated_at=excluded.updated_at""",
            (json.dumps(payload, sort_keys=True), self.clock.now().isoformat()),
        )
        self.database.connection.commit()


class RuntimeStatePersistenceRepository:
    """Atomically persist normalized snapshots and StateStore metadata."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def persist(
        self, snapshots: Sequence[StateSnapshot], metadata: StateStoreMetadata
    ) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            for snapshot in snapshots:
                connection.execute(
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
            self._save_metadata_without_commit(connection, metadata)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def delete(self, device_id: str, metadata: StateStoreMetadata) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM state_snapshots WHERE device_id = ?", (device_id,))
            self._save_metadata_without_commit(connection, metadata)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _save_metadata_without_commit(
        self, connection: sqlite3.Connection, metadata: StateStoreMetadata
    ) -> None:
        payload = {
            "inventory_revision": metadata.inventory_revision,
            "version_counter": metadata.version_counter,
            "state_versions": {
                f"{device_id}::{capability}": version
                for (device_id, capability), version in metadata.state_versions.items()
            },
            "inventory_fingerprint": metadata.inventory_fingerprint,
        }
        connection.execute(
            """INSERT INTO runtime_state_metadata (id, payload, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               payload=excluded.payload, updated_at=excluded.updated_at""",
            (json.dumps(payload, sort_keys=True), self.clock.now().isoformat()),
        )


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
    _RECONCILABLE_STATUSES = frozenset({"executed", "failed", "unknown", "cancelled"})

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def schedule(self, plan: Plan) -> None:
        if plan.execute_at is None:
            raise ValueError("plan.execute_at is required to schedule a plan")
        scheduled_plan = plan
        if plan.execution_window is None:
            scheduled_plan = plan.model_copy(
                update={
                    "execution_window": ExecutionWindow(
                        intended_at=plan.execute_at,
                        not_before=plan.execute_at,
                        not_after=plan.execute_at,
                        timezone=getattr(plan.execute_at.tzinfo, "key", None)
                        or plan.execute_at.tzname()
                        or "UTC",
                        revision=plan.schedule_revision,
                    )
                }
            )
        now = self.clock.now().isoformat()
        self.database.connection.execute(
            """INSERT INTO scheduled_plans
               (plan_id, execute_at, status, payload, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (
                plan.id,
                plan.execute_at.isoformat(),
                json.dumps(scheduled_plan.model_dump(mode="json"), sort_keys=True),
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

    async def mark_executed(self, plan_id: str) -> bool:
        return await self.reconcile_terminal(plan_id, "executed")

    async def reconcile_terminal(self, plan_id: str, status: str) -> bool:
        """Converge a pending row to a terminal status without overwriting decisions."""
        if status not in self._RECONCILABLE_STATUSES:
            raise ValueError(f"Unsupported terminal scheduled-plan status: {status}")
        if await self._transition(plan_id, status):
            return True
        existing = await self.get(plan_id)
        return existing is not None and existing[1] == status

    async def mark_missed(self, plan_id: str) -> None:
        await self._transition(plan_id, "missed")

    async def cancel(self, plan_id: str) -> bool:
        return await self._transition(plan_id, "cancelled")

    async def reschedule(
        self,
        plan_id: str,
        execute_at: datetime,
        *,
        expected_revision: int | None = None,
        expected_validation_digest: str | None = None,
        replacement_plan: Plan | None = None,
    ) -> bool:
        """Replace pending temporal evidence only through a validated CAS.

        The historical two-argument form is intentionally inert. Moving only
        ``execute_at`` would preserve an approval for a different physical
        intent, so callers must provide the complete replacement plan and the
        evidence they observed when making the request.
        """

        if (
            expected_revision is None
            or expected_validation_digest is None
            or replacement_plan is None
        ):
            return False
        if replacement_plan.id != plan_id or replacement_plan.execute_at != execute_at:
            return False
        if replacement_plan.execution_window is None or replacement_plan.validation is None:
            return False
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload, status FROM scheduled_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None or row[1] != "pending":
                connection.rollback()
                return False
            stored = Plan.model_validate(json.loads(row[0]))
            if stored.schedule_revision != expected_revision:
                connection.rollback()
                return False
            if stored.validation is None or stored.validation.digest != expected_validation_digest:
                connection.rollback()
                return False
            if replacement_plan.schedule_revision != stored.schedule_revision + 1:
                connection.rollback()
                return False
            if replacement_plan.validation.digest == stored.validation.digest:
                connection.rollback()
                return False
            cursor = connection.execute(
                """UPDATE scheduled_plans SET execute_at = ?, payload = ?, updated_at = ?
                   WHERE plan_id = ? AND status = 'pending'
                     AND json_extract(payload, '$.schedule_revision') = ?
                     AND json_extract(payload, '$.validation.digest') = ?""",
                (
                    execute_at.isoformat(),
                    json.dumps(replacement_plan.model_dump(mode="json"), sort_keys=True),
                    self.clock.now().isoformat(),
                    plan_id,
                    expected_revision,
                    expected_validation_digest,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            connection.rollback()
            raise

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
