"""Durable multi-host coordination records."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any
from uuid import uuid4

from domoai.domain.coordination import (
    PhysicalIntent,
    PhysicalIntentClaim,
    PhysicalIntentClaimDecision,
    PhysicalIntentStatus,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, SystemClock

_SAFE_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_:]{0,127}$")
_FORBIDDEN_LABEL_WORDS = ("token", "bearer", "secret", "payload", "claim")
_SETTLEMENT_TRANSITIONS = {
    PhysicalIntentStatus.DISPATCHING: frozenset(
        {
            PhysicalIntentStatus.ACKNOWLEDGED,
            PhysicalIntentStatus.CONFIRMED,
            PhysicalIntentStatus.REJECTED,
            PhysicalIntentStatus.UNKNOWN,
        }
    ),
    PhysicalIntentStatus.ACKNOWLEDGED: frozenset(
        {
            PhysicalIntentStatus.CONFIRMED,
            PhysicalIntentStatus.REJECTED,
            PhysicalIntentStatus.UNKNOWN,
        }
    ),
}


class PhysicalIntentRepository:
    """Unique household/idempotency ledger for physical dispatch."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def claim_for_dispatch(self, intent: PhysicalIntent) -> PhysicalIntentClaim:
        connection = self.database.connection
        dispatching = PhysicalIntent.model_validate(
            intent.model_dump(mode="python")
            | {"status": PhysicalIntentStatus.DISPATCHING}
        )
        payload = json.dumps(dispatching.model_dump(mode="json"), sort_keys=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT payload, status, owner_id, execution_attempt_id, fencing_epoch,
                          created_at, updated_at FROM physical_intents
                   WHERE household_id = ? AND idempotency_key = ?""",
                (intent.household_id, intent.idempotency_key),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO physical_intents
                       (household_id, idempotency_key, payload, status, fencing_epoch,
                        owner_id, execution_attempt_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dispatching.household_id,
                        dispatching.idempotency_key,
                        payload,
                        dispatching.status.value,
                        dispatching.fencing_epoch,
                        dispatching.owner_id,
                        dispatching.execution_attempt_id,
                        dispatching.created_at.isoformat(),
                        dispatching.updated_at.isoformat(),
                    ),
                )
                connection.commit()
                return PhysicalIntentClaim(
                    decision=PhysicalIntentClaimDecision.ACQUIRED,
                    intent=dispatching,
                )

            stored = self._read_consistent(
                row, household_id=intent.household_id, idempotency_key=intent.idempotency_key
            )
            connection.commit()
            if not self._same_logical_identity(stored, intent):
                decision = PhysicalIntentClaimDecision.CONFLICT
            elif stored.status in {
                PhysicalIntentStatus.ACKNOWLEDGED,
                PhysicalIntentStatus.CONFIRMED,
                PhysicalIntentStatus.REJECTED,
            }:
                decision = PhysicalIntentClaimDecision.REPLAY
            else:
                decision = PhysicalIntentClaimDecision.IN_FLIGHT
            return PhysicalIntentClaim(decision=decision, intent=stored)
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _read_consistent(row: Any, *, household_id: str, idempotency_key: str) -> PhysicalIntent:
        current = PhysicalIntent.model_validate(json.loads(row[0]))
        if (
            tuple(row[1:5])
            != (
                current.status.value,
                current.owner_id,
                current.execution_attempt_id,
                current.fencing_epoch,
            )
            or datetime.fromisoformat(row[5]) != current.created_at
            or datetime.fromisoformat(row[6]) != current.updated_at
            or current.household_id != household_id
            or current.idempotency_key != idempotency_key
        ):
            raise ValueError("physical intent projection is inconsistent with payload")
        return current

    @staticmethod
    def _same_logical_identity(left: PhysicalIntent, right: PhysicalIntent) -> bool:
        return (
            left.tenant_id,
            left.household_id,
            left.deployment_id,
            left.idempotency_key,
            left.plan_id,
            left.command_id,
        ) == (
            right.tenant_id,
            right.household_id,
            right.deployment_id,
            right.idempotency_key,
            right.plan_id,
            right.command_id,
        )

    async def get(self, *, household_id: str, idempotency_key: str) -> PhysicalIntent | None:
        row = self.database.connection.execute(
            """SELECT payload, status, owner_id, execution_attempt_id, fencing_epoch,
                          created_at, updated_at FROM physical_intents
               WHERE household_id = ? AND idempotency_key = ?""",
            (household_id, idempotency_key),
        ).fetchone()
        return (
            self._read_consistent(row, household_id=household_id, idempotency_key=idempotency_key)
            if row is not None
            else None
        )

    async def settle(
        self,
        *,
        intent: PhysicalIntent,
        status: PhysicalIntentStatus,
    ) -> PhysicalIntent:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            attempted_states: set[PhysicalIntentStatus] = set()
            while True:
                row = connection.execute(
                    """SELECT payload, status, owner_id, execution_attempt_id, fencing_epoch,
                              created_at, updated_at
                       FROM physical_intents
                       WHERE household_id = ? AND idempotency_key = ?""",
                    (intent.household_id, intent.idempotency_key),
                ).fetchone()
                if row is None:
                    raise KeyError("physical intent not found")
                current = PhysicalIntent.model_validate(json.loads(row[0]))
                if (
                    tuple(row[1:5])
                    != (
                        current.status.value,
                        current.owner_id,
                        current.execution_attempt_id,
                        current.fencing_epoch,
                    )
                    or datetime.fromisoformat(row[5]) != current.created_at
                    or datetime.fromisoformat(row[6]) != current.updated_at
                    or current.household_id != intent.household_id
                    or current.idempotency_key != intent.idempotency_key
                ):
                    raise ValueError("physical intent projection is inconsistent with payload")
                if (
                    status not in _SETTLEMENT_TRANSITIONS.get(current.status, frozenset())
                    or not self._same_logical_identity(current, intent)
                    or current.owner_id != intent.owner_id
                    or current.execution_attempt_id != intent.execution_attempt_id
                    or current.fencing_epoch != intent.fencing_epoch
                    or current.lease_id != intent.lease_id
                ):
                    connection.commit()
                    return current
                # A lost CAS can only retry after monotonic progress, bounding retries
                # to DISPATCHING -> ACKNOWLEDGED before the next read is terminal.
                if current.status in attempted_states:
                    raise ValueError("physical intent changed without settlement progress")
                attempted_states.add(current.status)
                updated = current.model_copy(
                    update={"status": status, "updated_at": self.clock.now()}
                )
                result = connection.execute(
                    """UPDATE physical_intents SET payload = ?, status = ?, updated_at = ?
                       WHERE household_id = ? AND idempotency_key = ?
                         AND payload = ? AND status = ? AND owner_id = ?
                         AND execution_attempt_id = ? AND fencing_epoch = ?
                         AND created_at = ? AND updated_at = ?""",
                    (
                        json.dumps(updated.model_dump(mode="json"), sort_keys=True),
                        updated.status.value,
                        updated.updated_at.isoformat(),
                        intent.household_id,
                        intent.idempotency_key,
                        *row,
                    ),
                )
                if result.rowcount == 1:
                    connection.commit()
                    return updated
        except BaseException:
            connection.rollback()
            raise

    async def recover_inflight(
        self,
        *,
        deployment_id: str | None = None,
        fencing_epoch: int | None = None,
    ) -> int:
        if fencing_epoch is not None and fencing_epoch <= 0:
            raise ValueError("fencing_epoch must be positive")
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            clauses = ["status IN ('prepared', 'dispatching', 'acknowledged')"]
            params: list[object] = []
            valid_json = (
                "payload IS JSON OBJECT"
                if hasattr(connection, "pgconn") else "json_valid(payload)"
            )
            deployment_filter = (
                f"CASE WHEN {valid_json} THEN json_extract(payload, '$.deployment_id') "
                "ELSE NULL END = ?"
            )
            if deployment_id is not None:
                clauses.append(deployment_filter)
                params.append(deployment_id)
            if fencing_epoch is not None:
                clauses.append("fencing_epoch < ?")
                params.append(fencing_epoch)
            rows = connection.execute(
                """SELECT household_id, idempotency_key, payload, status, owner_id,
                          execution_attempt_id, fencing_epoch, created_at, updated_at
                   FROM physical_intents
                   WHERE """
                + " AND ".join(clauses),
                params,
            ).fetchall()
            recovered = 0
            for household_id, idempotency_key, *stored_row in rows:
                try:
                    current = self._read_consistent(
                        stored_row, household_id=household_id, idempotency_key=idempotency_key
                    )
                except (ValueError, TypeError):
                    # An incompatible row is evidence, not a recoverable dispatch.
                    # Keep its original payload/projections and continue other work.
                    continue
                stored_epoch = current.fencing_epoch
                intent = current.model_copy(
                    update={"status": PhysicalIntentStatus.UNKNOWN, "updated_at": self.clock.now()}
                )
                update_clauses = [
                    "household_id = ?",
                    "idempotency_key = ?",
                    "status IN ('prepared', 'dispatching', 'acknowledged')",
                    "fencing_epoch = ?",
                ]
                update_params: list[object] = [
                    household_id,
                    idempotency_key,
                    stored_epoch,
                ]
                if deployment_id is not None:
                    update_clauses.append(deployment_filter)
                    update_params.append(deployment_id)
                if fencing_epoch is not None:
                    update_clauses.append("fencing_epoch < ?")
                    update_params.append(fencing_epoch)
                result = connection.execute(
                    """UPDATE physical_intents
                       SET payload = ?, status = 'unknown', updated_at = ?
                       WHERE """
                    + " AND ".join(update_clauses),
                    (
                        json.dumps(intent.model_dump(mode="json"), sort_keys=True),
                        intent.updated_at.isoformat(),
                        *update_params,
                    ),
                )
                recovered += int(result.rowcount == 1)
            connection.commit()
            return recovered
        except BaseException:
            connection.rollback()
            raise

    async def count(self) -> int:
        row = self.database.connection.execute("SELECT COUNT(*) FROM physical_intents").fetchone()
        return int(row[0]) if row is not None else 0


class MetricHistoryRepository:
    """Bounded historical samples for one runtime instance."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def append(
        self,
        *,
        instance_id: str,
        process_start_time: datetime,
        metric_name: str,
        value: float,
        labels: dict[str, str] | None = None,
        max_samples: int = 1000,
    ) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        if not _SAFE_METRIC.fullmatch(metric_name):
            raise ValueError("metric_name is not safe")
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
        if process_start_time.tzinfo is None or process_start_time.utcoffset() is None:
            raise ValueError("process_start_time must be timezone-aware")
        normalized_labels = labels or {}
        if len(normalized_labels) > 8 or any(
            any(word in key.lower() for word in _FORBIDDEN_LABEL_WORDS)
            for key in normalized_labels
        ):
            raise ValueError("metric labels contain forbidden or excessive fields")
        if any(len(key) > 64 or len(value) > 128 for key, value in normalized_labels.items()):
            raise ValueError("metric labels are too long")
        recorded_at = self.clock.now()
        payload = json.dumps(normalized_labels, sort_keys=True)
        self.database.connection.execute(
            """INSERT INTO operational_metric_history
               (sample_id, instance_id, process_start_time, metric_name, value, labels, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid4().hex,
                instance_id,
                process_start_time.isoformat(),
                metric_name,
                value,
                payload,
                recorded_at.isoformat(),
            ),
        )
        self.database.connection.execute(
            """DELETE FROM operational_metric_history
               WHERE sample_id IN (
                 SELECT sample_id FROM operational_metric_history
                 WHERE instance_id = ? ORDER BY recorded_at DESC LIMIT -1 OFFSET ?
               )""",
            (instance_id, max_samples),
        )
        self.database.connection.commit()

    async def count(self, *, instance_id: str) -> int:
        row = self.database.connection.execute(
            "SELECT COUNT(*) FROM operational_metric_history WHERE instance_id = ?",
            (instance_id,),
        ).fetchone()
        return int(row[0]) if row is not None else 0
