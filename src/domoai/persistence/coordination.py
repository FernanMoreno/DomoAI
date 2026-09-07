"""Durable multi-host coordination records."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from uuid import uuid4

from domoai.domain.coordination import PhysicalIntent, PhysicalIntentStatus
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, SystemClock

_SAFE_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_:]{0,127}$")
_FORBIDDEN_LABEL_WORDS = ("token", "bearer", "secret", "payload", "claim")


class PhysicalIntentRepository:
    """Unique household/idempotency ledger for physical dispatch."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def claim(self, intent: PhysicalIntent) -> PhysicalIntent:
        connection = self.database.connection
        payload = json.dumps(intent.model_dump(mode="json"), sort_keys=True)
        connection.execute(
            """INSERT INTO physical_intents
               (household_id, idempotency_key, payload, status, fencing_epoch,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(household_id, idempotency_key) DO NOTHING""",
            (
                intent.household_id,
                intent.idempotency_key,
                payload,
                intent.status.value,
                intent.fencing_epoch,
                intent.created_at.isoformat(),
                intent.updated_at.isoformat(),
            ),
        )
        connection.commit()
        return await self.get(
            household_id=intent.household_id, idempotency_key=intent.idempotency_key
        ) or intent

    async def get(self, *, household_id: str, idempotency_key: str) -> PhysicalIntent | None:
        row = self.database.connection.execute(
            """SELECT payload FROM physical_intents
               WHERE household_id = ? AND idempotency_key = ?""",
            (household_id, idempotency_key),
        ).fetchone()
        return PhysicalIntent.model_validate(json.loads(row[0])) if row is not None else None

    async def settle(
        self,
        *,
        household_id: str,
        idempotency_key: str,
        status: PhysicalIntentStatus,
    ) -> PhysicalIntent:
        current = await self.get(household_id=household_id, idempotency_key=idempotency_key)
        if current is None:
            raise KeyError("physical intent not found")
        if current.status not in {
            PhysicalIntentStatus.PREPARED,
            PhysicalIntentStatus.ACKNOWLEDGED,
        }:
            return current
        updated = current.model_copy(update={"status": status, "updated_at": self.clock.now()})
        self.database.connection.execute(
            """UPDATE physical_intents SET payload = ?, status = ?, updated_at = ?
               WHERE household_id = ? AND idempotency_key = ?
                 AND status IN ('prepared', 'acknowledged')""",
            (
                json.dumps(updated.model_dump(mode="json"), sort_keys=True),
                updated.status.value,
                updated.updated_at.isoformat(),
                household_id,
                idempotency_key,
            ),
        )
        self.database.connection.commit()
        return await self.get(household_id=household_id, idempotency_key=idempotency_key) or updated

    async def recover_inflight(self) -> int:
        rows = self.database.connection.execute(
            """SELECT household_id, idempotency_key, payload FROM physical_intents
               WHERE status IN ('prepared', 'acknowledged')"""
        ).fetchall()
        recovered = 0
        for household_id, idempotency_key, payload in rows:
            intent = PhysicalIntent.model_validate(json.loads(payload)).model_copy(
                update={"status": PhysicalIntentStatus.UNKNOWN, "updated_at": self.clock.now()}
            )
            self.database.connection.execute(
                """UPDATE physical_intents SET payload = ?, status = 'unknown', updated_at = ?
                   WHERE household_id = ? AND idempotency_key = ?
                     AND status IN ('prepared', 'acknowledged')""",
                (
                    json.dumps(intent.model_dump(mode="json"), sort_keys=True),
                    intent.updated_at.isoformat(),
                    household_id,
                    idempotency_key,
                ),
            )
            recovered += 1
        self.database.connection.commit()
        return recovered

    async def count(self) -> int:
        row = self.database.connection.execute(
            "SELECT COUNT(*) FROM physical_intents"
        ).fetchone()
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
