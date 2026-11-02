"""SQLite repository for secret-safe connection profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from domoai.domain.connections import ConnectionProfile
from domoai.domain.coordination import LeaseScope
from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.clock import Clock, SystemClock

_SELECT_COLUMNS = """
    id, connector_id, unique_id, endpoint_json, options_json,
    credential_ref, mapping_ref, enabled, schema_version, created_at, updated_at
"""


_OWNED = """EXISTS (SELECT 1 FROM connection_ownership AS ownership
    WHERE ownership.connection_id = connections.id
    AND ownership.tenant_id = ? AND ownership.household_id = ?
    AND ownership.deployment_id = ?)"""


@dataclass(frozen=True)
class SecretTransfer:
    transfer_id: UUID
    secret_ref: str
    profile_id: UUID
    connector_id: str
    unique_id: str
    envelope_digest: str
    operation: str = "create"
    expected_old_ref: str | None = None
    state: str = "pending"


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

    def __init__(
        self, database: SQLiteDatabase, *, scope: LeaseScope, clock: Clock | None = None
    ) -> None:
        self.database = database
        self._scope = (scope.tenant_id, scope.household_id, scope.deployment_id)
        self.clock = clock or SystemClock()

    async def prepare_secret_transfer(self, transfer: SecretTransfer) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Secret-transfer metadata requires at least the floor3 schema.
            # Bootstrap separately rejects floors unsupported by this reader.
            floor = connection.execute(
                "SELECT minimum_reader FROM runtime_contract_floor WHERE singleton = 1"
            ).fetchone()
            if floor is None or floor[0] < 3:
                raise ValueError("secret transfer reader floor is unavailable")
            now = self.clock.now().isoformat()
            connection.execute(
                """INSERT INTO connection_secret_transfers
                (transfer_id, tenant_id, household_id, deployment_id, secret_ref,
                 profile_id, connector_id, unique_id, operation, expected_old_ref,
                 envelope_digest, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    str(transfer.transfer_id),
                    *self._scope,
                    transfer.secret_ref,
                    str(transfer.profile_id),
                    transfer.connector_id,
                    transfer.unique_id,
                    transfer.operation,
                    transfer.expected_old_ref,
                    transfer.envelope_digest,
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def has_unresolved_secret_transfers(self) -> bool:
        row = self.database.connection.execute(
            """SELECT 1 FROM connection_secret_transfers
            WHERE tenant_id = ? AND household_id = ? AND deployment_id = ?
            AND state IN ('pending', 'discarding') LIMIT 1""",
            self._scope,
        ).fetchone()
        return row is not None

    async def get_secret_transfer(self, transfer_id: UUID) -> SecretTransfer | None:
        row = self.database.connection.execute(
            """SELECT transfer_id, secret_ref, profile_id,
            connector_id, unique_id, envelope_digest, operation, expected_old_ref, state
            FROM connection_secret_transfers WHERE transfer_id = ?
            AND tenant_id = ? AND household_id = ? AND deployment_id = ?""",
            (str(transfer_id), *self._scope),
        ).fetchone()
        if row is None:
            return None
        return SecretTransfer(UUID(row[0]), row[1], UUID(row[2]), *row[3:])

    async def adopt_secret_transfer(self, profile: ConnectionProfile, transfer_id: UUID) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            transfer = await self.get_secret_transfer(transfer_id)
            if (
                transfer is None
                or transfer.profile_id != profile.id
                or transfer.secret_ref != profile.credential_ref
                or (transfer.connector_id, transfer.unique_id)
                != (profile.connector_id, profile.unique_id)
            ):
                raise ValueError("secret transfer does not match profile")
            changed = connection.execute(
                """UPDATE connection_secret_transfers SET state = 'adopted', updated_at = ?
                WHERE transfer_id = ? AND tenant_id = ? AND household_id = ? AND deployment_id = ?
                AND state = 'pending'""",
                (self.clock.now().isoformat(), str(transfer_id), *self._scope),
            ).rowcount
            if changed != 1:
                raise ValueError("secret transfer is not pending")
            if transfer.operation == "create":
                connection.execute(
                    f"INSERT INTO connections ({_SELECT_COLUMNS}, credential_transfer_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*_serialize_profile(profile), str(transfer_id)),
                )
                connection.execute(
                    """INSERT INTO connection_ownership
                    (connection_id, tenant_id, household_id, deployment_id, bound_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (str(profile.id), *self._scope, self.clock.now().isoformat()),
                )
            else:
                changed = connection.execute(
                    f"""UPDATE connections SET credential_ref = ?, updated_at = ?,
                    credential_transfer_id = ?
                    WHERE id = ? AND {_OWNED} AND connector_id = ? AND unique_id = ? AND
                    (credential_ref = ? OR (credential_ref IS NULL AND CAST(? AS TEXT) IS NULL))""",
                    (
                        profile.credential_ref,
                        profile.updated_at.isoformat(),
                        str(transfer_id),
                        str(profile.id),
                        *self._scope,
                        transfer.connector_id,
                        transfer.unique_id,
                        transfer.expected_old_ref,
                        transfer.expected_old_ref,
                    ),
                ).rowcount
                if changed != 1:
                    raise ValueError("credential reference changed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _transfer_row_lock(self) -> str:
        return " FOR UPDATE" if isinstance(self.database, PostgresDatabase) else ""

    def _check_borrowed_reference(self, reference: str | None) -> None:
        if reference is None:
            return
        row = self.database.connection.execute(
            "SELECT state FROM connection_secret_transfers WHERE secret_ref = ?"
            + self._transfer_row_lock(),
            (reference,),
        ).fetchone()
        if row is not None and row[0] != "adopted":
            raise ValueError("credential reference is in transfer")

    def _reference_in_use(self, reference: str) -> bool:
        # All households veto removal. Ownership limits authority, not the
        # set of references that can make deleting this file unsafe.
        return (
            self.database.connection.execute(
                "SELECT 1 FROM connections WHERE credential_ref = ? LIMIT 1",
                (reference,),
            ).fetchone()
            is not None
        )

    async def begin_secret_discard(self, transfer: SecretTransfer) -> bool:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "SELECT transfer_id FROM connection_secret_transfers WHERE transfer_id = ?"
                + self._transfer_row_lock(),
                (str(transfer.transfer_id),),
            ).fetchone()
            observed = await self.get_secret_transfer(transfer.transfer_id)
            if (
                observed is None
                or observed.state not in {"pending", "discarding"}
                or replace(observed, state="pending") != transfer
                or self._reference_in_use(transfer.secret_ref)
            ):
                connection.rollback()
                return False
            changed = connection.execute(
                """UPDATE connection_secret_transfers SET state = 'discarding', updated_at = ?
                WHERE transfer_id = ? AND tenant_id = ? AND household_id = ? AND deployment_id = ?
                AND secret_ref = ? AND envelope_digest = ?
                AND state IN ('pending', 'discarding')""",
                (
                    self.clock.now().isoformat(),
                    str(transfer.transfer_id),
                    *self._scope,
                    transfer.secret_ref,
                    transfer.envelope_digest,
                ),
            ).rowcount
            connection.commit()
            return changed == 1
        except Exception:
            connection.rollback()
            raise

    async def finish_secret_discard(self, transfer: SecretTransfer) -> bool:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "SELECT transfer_id FROM connection_secret_transfers WHERE transfer_id = ?"
                + self._transfer_row_lock(),
                (str(transfer.transfer_id),),
            ).fetchone()
            observed = await self.get_secret_transfer(transfer.transfer_id)
            if (
                observed is None
                or observed.state not in {"discarding", "discarded"}
                or replace(observed, state="pending") != transfer
                or self._reference_in_use(transfer.secret_ref)
            ):
                connection.rollback()
                return False
            connection.execute(
                """UPDATE connection_secret_transfers SET state = 'discarded', updated_at = ?
                WHERE transfer_id = ? AND tenant_id = ? AND household_id = ? AND deployment_id = ?
                AND state = 'discarding'""",
                (self.clock.now().isoformat(), str(transfer.transfer_id), *self._scope),
            )
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise

    async def save(self, profile: ConnectionProfile) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._check_borrowed_reference(profile.credential_ref)
            connection.execute(
                f"""INSERT INTO connections ({_SELECT_COLUMNS})
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                _serialize_profile(profile),
            )
            connection.execute(
                """INSERT INTO connection_ownership
                   (connection_id, tenant_id, household_id, deployment_id, bound_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (str(profile.id), *self._scope, self.clock.now().isoformat()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def get(self, connection_id: UUID) -> ConnectionProfile | None:
        cursor = self.database.connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM connections WHERE id = ? AND {_OWNED}",
            (str(connection_id), *self._scope),
        )
        row = cursor.fetchone()
        cursor.close()
        return _deserialize_profile(tuple(row)) if row is not None else None

    async def get_credential_transfer_id(self, connection_id: UUID) -> str | None:
        row = self.database.connection.execute(
            f"SELECT credential_transfer_id FROM connections WHERE id = ? AND {_OWNED}",
            (str(connection_id), *self._scope),
        ).fetchone()
        if row is None:
            raise ValueError("connection is unavailable")
        return cast(str | None, row[0])

    async def get_by_unique_id(self, connector_id: str, unique_id: str) -> ConnectionProfile | None:
        cursor = self.database.connection.execute(
            f"""SELECT {_SELECT_COLUMNS} FROM connections
                WHERE connector_id = ? AND unique_id = ? AND {_OWNED}""",
            (connector_id, unique_id, *self._scope),
        )
        row = cursor.fetchone()
        cursor.close()
        return _deserialize_profile(tuple(row)) if row is not None else None

    async def list_all(self) -> list[ConnectionProfile]:
        cursor = self.database.connection.execute(
            f"""SELECT {_SELECT_COLUMNS} FROM connections WHERE {_OWNED}
                ORDER BY created_at ASC, id ASC""",
            self._scope,
        )
        rows = cursor.fetchall()
        cursor.close()
        return [_deserialize_profile(tuple(row)) for row in rows]

    async def delete(self, connection_id: UUID) -> bool:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                f"DELETE FROM connections WHERE id = ? AND {_OWNED}",
                (str(connection_id), *self._scope),
            )
            deleted = cursor.rowcount > 0
            cursor.close()
            if deleted:
                connection.execute(
                    """DELETE FROM connection_ownership WHERE connection_id = ?
                       AND tenant_id = ? AND household_id = ? AND deployment_id = ?""",
                    (str(connection_id), *self._scope),
                )
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
                f"""UPDATE connections
                   SET enabled = ?, updated_at = ?
                   WHERE id = ? AND {_OWNED}""",
                (int(enabled), updated_at.isoformat(), str(connection_id), *self._scope),
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
            self._check_borrowed_reference(credential_ref)
            cursor = connection.execute(
                f"""UPDATE connections
                   SET credential_transfer_id = CASE
                       WHEN credential_ref = ?
                       OR (credential_ref IS NULL AND CAST(? AS TEXT) IS NULL)
                       THEN credential_transfer_id ELSE NULL END,
                       credential_ref = ?, updated_at = ?
                   WHERE id = ? AND {_OWNED}""",
                (
                    credential_ref,
                    credential_ref,
                    credential_ref,
                    updated_at.isoformat(),
                    str(connection_id),
                    *self._scope,
                ),
            )
            changed = cursor.rowcount > 0
            cursor.close()
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise

    async def compare_and_set_credential_ref(
        self, profile: ConnectionProfile, expected_old_ref: str | None
    ) -> bool:
        """Borrow a durable ref without adopting it or overwriting a concurrent change."""
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._check_borrowed_reference(profile.credential_ref)
            changed = connection.execute(
                f"""UPDATE connections SET credential_transfer_id = CASE
                WHEN credential_ref = ? OR (credential_ref IS NULL AND CAST(? AS TEXT) IS NULL)
                THEN credential_transfer_id ELSE NULL END,
                credential_ref = ?, updated_at = ?
                WHERE id = ? AND {_OWNED} AND connector_id = ? AND unique_id = ? AND
                (credential_ref = ? OR (credential_ref IS NULL AND CAST(? AS TEXT) IS NULL))""",
                (
                    profile.credential_ref,
                    profile.credential_ref,
                    profile.credential_ref,
                    profile.updated_at.isoformat(),
                    str(profile.id),
                    *self._scope,
                    profile.connector_id,
                    profile.unique_id,
                    expected_old_ref,
                    expected_old_ref,
                ),
            ).rowcount
            connection.commit()
            return changed == 1
        except Exception:
            connection.rollback()
            raise

    async def update(self, profile: ConnectionProfile) -> bool:
        """Atomically replace mutable profile metadata while retaining its ID."""

        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._check_borrowed_reference(profile.credential_ref)
            payload = _serialize_profile(profile)
            cursor = connection.execute(
                f"""UPDATE connections SET
                    credential_transfer_id = CASE
                    WHEN credential_ref = ? OR (credential_ref IS NULL AND CAST(? AS TEXT) IS NULL)
                    THEN credential_transfer_id ELSE NULL END,
                    connector_id = ?, unique_id = ?, endpoint_json = ?, options_json = ?,
                    credential_ref = ?, mapping_ref = ?, enabled = ?, schema_version = ?,
                    created_at = ?, updated_at = ?
                    WHERE id = ? AND {_OWNED}""",
                (
                    profile.credential_ref,
                    profile.credential_ref,
                    *payload[1:],
                    payload[0],
                    *self._scope,
                ),
            )
            changed = cursor.rowcount > 0
            cursor.close()
            connection.commit()
            return changed
        except Exception:
            connection.rollback()
            raise


__all__ = ["ConnectionRepository"]
