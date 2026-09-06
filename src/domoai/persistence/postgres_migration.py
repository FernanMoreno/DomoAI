"""Explicit, verifiable SQLite-to-PostgreSQL control-plane migration."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.sqlite import SQLiteDatabase

_TABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("devices", ("id", "payload", "updated_at"), ("id",)),
    (
        "state_snapshots",
        ("device_id", "capability", "payload", "observed_at"),
        ("device_id", "capability"),
    ),
    ("policies", ("id", "payload", "updated_at"), ("id",)),
    ("plans", ("id", "payload", "updated_at", "status"), ("id",)),
    (
        "execution_outcomes",
        ("plan_id", "command_id", "payload", "completed_at"),
        ("plan_id", "command_id"),
    ),
    (
        "audit_events",
        ("id", "event_type", "actor", "subject_id", "payload", "created_at", "authority_payload"),
        ("id",),
    ),
    (
        "execution_attempts",
        ("attempt_id", "plan_id", "command_id", "payload", "completed_at"),
        ("attempt_id",),
    ),
    ("scheduled_plans", ("plan_id", "execute_at", "status", "payload", "updated_at"), ("plan_id",)),
    (
        "recurring_schedules",
        (
            "schedule_id",
            "template_payload",
            "recurrence_payload",
            "next_execute_at",
            "status",
            "updated_at",
            "authority_payload",
        ),
        ("schedule_id",),
    ),
    ("bundle_commits", ("id", "bundle_digest", "status", "payload", "updated_at"), ("id",)),
    ("runtime_state_metadata", ("id", "payload", "updated_at"), ("id",)),
    (
        "runtime_ownership",
        (
            "deployment_id",
            "owner_id",
            "config_digest",
            "acquired_at",
            "released_at",
            "status",
            "uncertain",
        ),
        ("deployment_id",),
    ),
    (
        "approval_grants",
        ("approval_id", "payload", "issued_at", "valid_until", "status", "consumed_at"),
        ("approval_id",),
    ),
    (
        "approval_reservations",
        ("approval_id", "reservation_id", "status", "created_at", "updated_at"),
        ("approval_id",),
    ),
    (
        "audit_outbox",
        (
            "sequence",
            "event_id",
            "event_type",
            "actor",
            "subject_id",
            "payload",
            "created_at",
            "status",
            "attempts",
            "last_error",
            "delivered_at",
            "authority_payload",
        ),
        ("sequence",),
    ),
    (
        "automation_rules",
        (
            "rule_id",
            "payload",
            "consent_payload",
            "status",
            "last_event_id",
            "last_fired_at",
            "updated_at",
        ),
        ("rule_id",),
    ),
    (
        "commissioning_qualifications",
        ("id", "status", "payload", "authority_payload", "updated_at"),
        ("id",),
    ),
    (
        "state_history",
        (
            "history_id",
            "household_id",
            "device_id",
            "capability",
            "payload",
            "observed_at",
            "received_at",
            "source_adapter_id",
            "source_external_id",
        ),
        ("history_id",),
    ),
    (
        "physical_intents",
        (
            "household_id",
            "idempotency_key",
            "payload",
            "status",
            "fencing_epoch",
            "created_at",
            "updated_at",
        ),
        ("household_id", "idempotency_key"),
    ),
    (
        "operational_metric_history",
        (
            "sample_id",
            "instance_id",
            "process_start_time",
            "metric_name",
            "value",
            "labels",
            "recorded_at",
        ),
        ("sample_id",),
    ),
)


class PostgresMigrationError(RuntimeError):
    """Base class for an explicit migration failure."""


class MigrationConflict(PostgresMigrationError):
    """The destination already contains data and cannot be overwritten."""


class MigrationVerificationError(PostgresMigrationError):
    """Source and destination row/digest verification differed."""


@dataclass(frozen=True)
class PostgresMigrationReport:
    table_counts: dict[str, int]
    payload_digests: dict[str, str]


def _digest(rows: list[tuple[Any, ...]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(tuple(row)).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _select(
    database: Any, table: str, columns: tuple[str, ...], order: tuple[str, ...]
) -> list[tuple[Any, ...]]:
    query = (
        f"SELECT {', '.join(columns)} FROM {table} "
        f"ORDER BY {', '.join(order)}"
    )
    cursor = database.connection.execute(query)
    try:
        return [tuple(row) for row in cursor.fetchall()]
    finally:
        cursor.close()


async def migrate_sqlite_to_postgres(
    source: SQLiteDatabase, destination: PostgresDatabase
) -> PostgresMigrationReport:
    """Copy all application tables into an empty PostgreSQL control plane.

    The destination is never truncated. A non-empty target or any verification
    mismatch rolls back the complete transaction, making retry behavior
    explicit and recoverable.
    """

    source_rows: dict[str, list[tuple[Any, ...]]] = {}
    try:
        for table, columns, order in _TABLES:
            source_rows[table] = _select(source, table, columns, order)
    except sqlite3.Error as error:
        raise PostgresMigrationError("source SQLite schema is incomplete") from error

    connection = destination.connection
    try:
        connection.execute("BEGIN")
        for table, columns, order in _TABLES:
            existing = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            if existing is not None and int(existing[0]) != 0:
                raise MigrationConflict(f"destination table {table} is not empty")
            rows = source_rows[table]
            if rows:
                placeholders = ", ".join("%s" for _ in columns)
                for row in rows:
                    connection.execute(
                        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                        row,
                    )
            destination_rows = _select(destination, table, columns, order)
            if len(destination_rows) != len(rows) or _digest(destination_rows) != _digest(rows):
                raise MigrationVerificationError(f"migration verification failed for {table}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise

    return PostgresMigrationReport(
        table_counts={table: len(rows) for table, rows in source_rows.items()},
        payload_digests={table: _digest(rows) for table, rows in source_rows.items()},
    )


__all__ = [
    "MigrationConflict",
    "MigrationVerificationError",
    "PostgresMigrationError",
    "PostgresMigrationReport",
    "migrate_sqlite_to_postgres",
]
