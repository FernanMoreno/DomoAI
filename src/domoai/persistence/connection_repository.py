"""SQLite repository for secret-safe connection profiles."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from domoai.domain.connections import ConnectionProfile
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, SystemClock

_SELECT_COLUMNS = """
    id, connector_id, unique_id, endpoint_json, options_json,
    credential_ref, mapping_ref, enabled, schema_version, created_at, updated_at
"""


def _serialize_profile(profile: ConnectionProfile) -> tuple[Any, ...]:
    payload = profile.model_dump(mode="json")
    return (
        str(profile.id),
        profile.connector_id,
        profile.unique_id,
        json.dumps(payload["endpoint"], sort_keys=True, allow_nan=False),
        json.dumps(payload["options"], sort_keys=True, allow_nan=False),
        profile.credential_ref,
        profile.mapping_ref,
        int(profile.enabled),
        profile.schema_version,
        profile.created_at.isoformat(),
        profile.updated_at.isoformat(),
    )


def _deserialize_profile(row: tuple[Any, ...]) -> ConnectionProfile:
    return ConnectionProfile.model_validate(
        {
            "id": row[0],
            "connector_id": row[1],
            "unique_id": row[2],
            "endpoint": json.loads(row[3]),
            "options": json.loads(row[4]),
            "credential_ref": row[5],
            "mapping_ref": row[6],
            "enabled": row[7],
            "schema_version": row[8],
            "created_at": row[9],
            "updated_at": row[10],
        }
    )


def _require_aware_datetime(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("connection updated_at must be timezone-aware")


class ConnectionRepository:
    """Durable CRUD boundary for P0.1 ``ConnectionProfile`` values.

    ``save`` is intentionally insert-only.  The database constraints reject
    both duplicate profile IDs and duplicate connector/unique identities.
    """

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def save(self, profile: ConnectionProfile) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""INSERT INTO connections ({_SELECT_COLUMNS})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _serialize_profile(profile),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def get(self, connection_id: UUID) -> ConnectionProfile | None:
        cursor = self.database.connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM connections WHERE id = ?",
            (str(connection_id),),
        )
        row = cursor.fetchone()
        cursor.close()
        return _deserialize_profile(tuple(row)) if row is not None else None

    async def get_by_unique_id(
        self, connector_id: str, unique_id: str
    ) -> ConnectionProfile | None:
        cursor = self.database.connection.execute(
            f"""SELECT {_SELECT_COLUMNS} FROM connections
                WHERE connector_id = ? AND unique_id = ?""",
            (connector_id, unique_id),
        )
        row = cursor.fetchone()
        cursor.close()
        return _deserialize_profile(tuple(row)) if row is not None else None

    async def list_all(self) -> list[ConnectionProfile]:
        cursor = self.database.connection.execute(
            f"""SELECT {_SELECT_COLUMNS} FROM connections
                ORDER BY created_at ASC, id ASC"""
        )
        rows = cursor.fetchall()
        cursor.close()
        return [_deserialize_profile(tuple(row)) for row in rows]

    async def delete(self, connection_id: UUID) -> bool:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM connections WHERE id = ?", (str(connection_id),)
            )
            deleted = cursor.rowcount > 0
            cursor.close()
            connection.commit()
            return deleted
        except Exception:
            connection.rollback()
            raise

    async def set_enabled(self, connection_id: UUID, enabled: bool) -> bool:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        updated_at = self.clock.now()
        _require_aware_datetime(updated_at)
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE connections
                   SET enabled = ?, updated_at = ?
                   WHERE id = ?""",
                (int(enabled), updated_at.isoformat(), str(connection_id)),
            )
            changed = cursor.rowcount > 0
            cursor.close()
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise

    async def set_credential_ref(self, connection_id: UUID, credential_ref: str | None) -> bool:
        """Change only the opaque credential pointer during reauthentication."""

        updated_at = self.clock.now()
        _require_aware_datetime(updated_at)
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """UPDATE connections
                   SET credential_ref = ?, updated_at = ?
                   WHERE id = ?""",
                (credential_ref, updated_at.isoformat(), str(connection_id)),
            )
            changed = cursor.rowcount > 0
            cursor.close()
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise

    async def update(self, profile: ConnectionProfile) -> bool:
        """Atomically replace mutable profile metadata while retaining its ID."""

        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            payload = _serialize_profile(profile)
            cursor = connection.execute(
                """UPDATE connections SET
                    connector_id = ?, unique_id = ?, endpoint_json = ?, options_json = ?,
                    credential_ref = ?, mapping_ref = ?, enabled = ?, schema_version = ?,
                    created_at = ?, updated_at = ?
                    WHERE id = ?""",
                (*payload[1:], payload[0]),
            )
            changed = cursor.rowcount > 0
            cursor.close()
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise


__all__ = ["ConnectionRepository"]
