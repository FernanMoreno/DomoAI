"""Scoped preference storage."""

import hashlib
import json
from datetime import datetime
from typing import Any, Never
from uuid import UUID, uuid4

from domoai.domain.errors import DomainError, ErrorCode
from domoai.domain.household_preferences import (
    HouseholdPreference,
    Operation,
    PreferenceHistoryEntry,
    PreferenceMutation,
    PreferenceProvenance,
    PreferenceRestore,
    PreferenceResult,
    PreferenceSlotMutation,
    PreferenceTarget,
    PreferenceVersion,
)
from domoai.persistence.sqlite import SQLiteDatabase


class HouseholdPreferenceRepository:
    def __init__(self, database: SQLiteDatabase, *, tenant_id: str, household_id: str) -> None:
        self.database = database
        self.tenant_id = tenant_id
        self.household_id = household_id

    async def bootstrap(self) -> None:
        self.database.connection.execute(
            "INSERT OR IGNORE INTO household_preference_heads "
            "(tenant_id, household_id, generation, revision) VALUES (?, ?, ?, 0)",
            (self.tenant_id, self.household_id, str(uuid4())),
        )
        self.database.connection.commit()

    @property
    def scope(self) -> tuple[str, str]:
        return self.tenant_id, self.household_id

    def _head(self) -> PreferenceVersion:
        row = self.database.connection.execute(
            "SELECT generation, revision FROM household_preference_heads "
            "WHERE tenant_id=? AND household_id=?",
            self.scope,
        ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Preference profile is unavailable")
        return PreferenceVersion(generation=row[0], revision=row[1])

    async def snapshot(self) -> tuple[PreferenceVersion, list[HouseholdPreference]]:
        connection = self.database.connection
        try:
            return self.snapshot_in_transaction(connection)
        finally:
            connection.rollback()  # close read transaction without writing anything

    def snapshot_in_transaction(
        self, connection: Any
    ) -> tuple[PreferenceVersion, list[HouseholdPreference]]:
        """Read the complete scope; the caller owns the connection, locks and transaction."""
        # One statement gives one consistent snapshot on PostgreSQL READ
        # COMMITTED too; separate head and slot SELECTs would permit tearing.
        rows = connection.execute(
            "SELECT h.generation, h.revision, p.payload FROM household_preference_heads h "
            "LEFT JOIN household_preferences p ON h.tenant_id=p.tenant_id "
            "AND h.household_id=p.household_id WHERE h.tenant_id=? AND h.household_id=? "
            "ORDER BY p.preference_id",
            self.scope,
        ).fetchall()
        if not rows:
            raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Preference profile is unavailable")
        return (
            PreferenceVersion(generation=rows[0][0], revision=rows[0][1]),
            [HouseholdPreference.model_validate_json(row[2]) for row in rows if row[2] is not None],
        )

    def _receipt(
        self,
        request: PreferenceMutation | PreferenceSlotMutation,
        principal_id: str,
        fingerprint: str,
    ) -> PreferenceResult | None:
        row = self.database.connection.execute(
            "SELECT fingerprint, response FROM household_preference_requests "
            "WHERE tenant_id=? AND household_id=? AND generation=? "
            "AND principal_id=? AND idempotency_key=?",
            (
                *self.scope,
                str(request.expected_version.generation),
                principal_id,
                request.idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if row[0] != fingerprint:
            raise DomainError(
                ErrorCode.IDEMPOTENCY_CONFLICT, "Idempotency key has different content"
            )
        return PreferenceResult.model_validate_json(row[1])

    async def replay(
        self,
        *,
        request: PreferenceMutation | PreferenceSlotMutation,
        principal_id: str,
        fingerprint: str,
    ) -> PreferenceResult | None:
        """A confirmed receipt is independent of subsequently expired intent."""
        try:
            head = self._head()
            if head.generation != request.expected_version.generation:
                self._conflict(head)
            return self._receipt(request, principal_id, fingerprint)
        finally:
            self.database.connection.rollback()

    async def mutate(
        self,
        *,
        operation: Operation,
        request: PreferenceMutation | PreferenceSlotMutation,
        principal_id: str,
        now: datetime,
        fingerprint: str | None = None,
    ) -> PreferenceResult:
        digest = (
            fingerprint
            or hashlib.sha256(
                (
                    operation
                    + ":"
                    + json.dumps(
                        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                    )
                ).encode()
            ).hexdigest()
        )
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = self._head()
            if head.generation != request.expected_version.generation:
                self._conflict(head)
            receipt = self._receipt(request, principal_id, digest)
            if receipt is not None:
                connection.rollback()
                return receipt
            changed = connection.execute(
                "UPDATE household_preference_heads SET revision=revision+1 "
                "WHERE tenant_id=? AND household_id=? AND generation=? AND revision=?",
                (
                    *self.scope,
                    str(request.expected_version.generation),
                    request.expected_version.revision,
                ),
            ).rowcount
            if changed != 1:
                head = self._head()
                if head.generation == request.expected_version.generation:
                    receipt = self._receipt(request, principal_id, digest)
                    if receipt is not None:
                        connection.rollback()
                        return receipt
                self._conflict(head)
            version = PreferenceVersion(
                generation=head.generation, revision=request.expected_version.revision + 1
            )
            if operation == "set":
                assert isinstance(request, PreferenceMutation)
                pref = request.preference
                old = connection.execute(
                    "SELECT payload FROM household_preferences "
                    "WHERE tenant_id=? AND household_id=? "
                    "AND key=? AND target_kind=? AND target_id=?",
                    (*self.scope, pref.key, pref.target.kind, pref.target.id),
                ).fetchone()
                slot = HouseholdPreference(
                    preference_id=(
                        HouseholdPreference.model_validate_json(old[0]).preference_id
                        if old
                        else uuid4()
                    ),
                    key=pref.key,
                    target=PreferenceTarget.model_validate(pref.target.model_dump()),
                    preference=pref,
                    provenance=PreferenceProvenance(
                        **pref.provenance.model_dump(), updated_by=principal_id, recorded_at=now
                    ),
                    version=version,
                    updated_at=now,
                )
            else:
                assert isinstance(request, PreferenceSlotMutation)
                row = connection.execute(
                    "SELECT payload FROM household_preferences "
                    "WHERE tenant_id=? AND household_id=? AND preference_id=?",
                    (*self.scope, str(request.preference_id)),
                ).fetchone()
                if row is None:
                    raise DomainError(ErrorCode.HISTORY_UNAVAILABLE, "Preference is unavailable")
                slot = HouseholdPreference.model_validate_json(row[0])
                if operation == "restore":
                    assert isinstance(request, PreferenceRestore)
                    source = self._history_source(request.preference_id, request.source_revision)
                    if (
                        source is None
                        or source.snapshot is None
                        or source.snapshot.preference is None
                    ):
                        raise DomainError(ErrorCode.HISTORY_UNAVAILABLE, "History is unavailable")
                    pref = source.snapshot.preference.model_copy(
                        update={
                            "valid_from": request.valid_from,
                            "valid_until": request.valid_until,
                            "timezone": request.timezone,
                            "provenance": request.provenance,
                        }
                    )
                    slot = slot.model_copy(
                        update={
                            "preference": pref,
                            "deleted": False,
                            "erased": False,
                            "provenance": PreferenceProvenance(
                                **request.provenance.model_dump(),
                                updated_by=principal_id,
                                recorded_at=now,
                            ),
                        }
                    )
                elif operation in {"delete", "erase"}:
                    slot = slot.model_copy(update={"deleted": True})
                    if operation == "erase":
                        slot = slot.model_copy(
                            update={"preference": None, "provenance": None, "erased": True}
                        )
                        self._erase_retained(slot.preference_id)
                else:
                    raise DomainError(
                        ErrorCode.INVALID_PREFERENCE, "Unsupported preference operation"
                    )
                slot = slot.model_copy(update={"version": version, "updated_at": now})
            self._write_slot(slot)
            entry = PreferenceHistoryEntry(
                version=version,
                operation=operation,
                preference_id=slot.preference_id,
                snapshot=None if operation == "erase" else slot,
                actor=principal_id,
                recorded_at=now,
                source_revision=request.source_revision
                if isinstance(request, PreferenceRestore)
                else None,
            )
            connection.execute(
                "INSERT INTO household_preference_history "
                "(tenant_id, household_id, revision, preference_id, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (*self.scope, version.revision, str(slot.preference_id), entry.model_dump_json()),
            )
            result = PreferenceResult(
                version=version,
                operation=operation,
                preference=None if operation == "erase" else slot,
                backups_may_retain_data=operation == "erase",
            )
            connection.execute(
                "INSERT INTO household_preference_requests (tenant_id, household_id, generation, "
                "principal_id, idempotency_key, fingerprint, preference_id, response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *self.scope,
                    str(version.generation),
                    principal_id,
                    request.idempotency_key,
                    digest,
                    str(slot.preference_id),
                    result.model_dump_json(),
                ),
            )
            connection.commit()
            return result
        except BaseException:
            connection.rollback()
            raise

    def _write_slot(self, slot: HouseholdPreference) -> None:
        self.database.connection.execute(
            "INSERT INTO household_preferences "
            "(tenant_id, household_id, preference_id, key, target_kind, target_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (tenant_id, household_id, preference_id) "
            "DO UPDATE SET payload=excluded.payload",
            (
                *self.scope,
                str(slot.preference_id),
                slot.key,
                slot.target.kind,
                slot.target.id,
                slot.model_dump_json(),
            ),
        )

    def _history_source(self, preference_id: UUID, revision: int) -> PreferenceHistoryEntry | None:
        row = self.database.connection.execute(
            "SELECT payload FROM household_preference_history WHERE tenant_id=? AND household_id=? "
            "AND preference_id=? AND revision=?",
            (*self.scope, str(preference_id), revision),
        ).fetchone()
        return PreferenceHistoryEntry.model_validate_json(row[0]) if row else None

    async def history_source(
        self, preference_id: UUID, revision: int
    ) -> PreferenceHistoryEntry | None:
        try:
            return self._history_source(preference_id, revision)
        finally:
            self.database.connection.rollback()

    async def history(
        self, *, after_revision: int = 0, limit: int = 100, preference_id: str | None = None
    ) -> tuple[PreferenceVersion, list[PreferenceHistoryEntry]]:
        try:
            # Include the head in the same statement for consistent pagination.
            rows = self.database.connection.execute(
                "SELECT h.generation, h.revision, p.payload FROM household_preference_heads h "
                "LEFT JOIN household_preference_history p ON h.tenant_id=p.tenant_id "
                "AND h.household_id=p.household_id AND p.revision>? "
                "AND (CAST(? AS TEXT) IS NULL OR p.preference_id=?) "
                "WHERE h.tenant_id=? AND h.household_id=? ORDER BY p.revision LIMIT ?",
                (after_revision, preference_id, preference_id, *self.scope, min(limit, 101)),
            ).fetchall()
            if not rows:
                raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Preference profile is unavailable")
            return (
                PreferenceVersion(generation=rows[0][0], revision=rows[0][1]),
                [PreferenceHistoryEntry.model_validate_json(row[2]) for row in rows if row[2]],
            )
        finally:
            self.database.connection.rollback()

    def _erase_retained(self, preference_id: UUID) -> None:
        connection = self.database.connection
        rows = connection.execute(
            "SELECT revision, payload FROM household_preference_history "
            "WHERE tenant_id=? AND household_id=? AND preference_id=?",
            (*self.scope, str(preference_id)),
        ).fetchall()
        for revision, payload in rows:
            entry = PreferenceHistoryEntry.model_validate_json(payload).model_copy(
                update={"snapshot": None}
            )
            connection.execute(
                "UPDATE household_preference_history SET payload=? "
                "WHERE tenant_id=? AND household_id=? AND revision=?",
                (entry.model_dump_json(), *self.scope, revision),
            )
        receipts = connection.execute(
            "SELECT generation, principal_id, idempotency_key, response "
            "FROM household_preference_requests "
            "WHERE tenant_id=? AND household_id=? AND preference_id=?",
            (*self.scope, str(preference_id)),
        ).fetchall()
        for generation, principal, key, payload in receipts:
            response = PreferenceResult.model_validate_json(payload).model_copy(
                update={"preference": None}
            )
            connection.execute(
                "UPDATE household_preference_requests SET response=? "
                "WHERE tenant_id=? AND household_id=? AND generation=? "
                "AND principal_id=? AND idempotency_key=?",
                (response.model_dump_json(), *self.scope, generation, principal, key),
            )

    @staticmethod
    def _conflict(version: PreferenceVersion) -> Never:
        raise DomainError(
            ErrorCode.PREFERENCE_CONFLICT,
            "Preference version changed",
            details={"current_version": version.model_dump(mode="json")},
        )


def rotate_restored_preference_generations(connection: Any) -> None:
    """Offline restore hook. Caller owns the transaction and traffic exclusion."""
    rows = connection.execute(
        "SELECT tenant_id, household_id FROM household_preference_heads"
    ).fetchall()
    for tenant_id, household_id in rows:
        connection.execute(
            "UPDATE household_preference_heads SET generation=? "
            "WHERE tenant_id=? AND household_id=?",
            (str(uuid4()), tenant_id, household_id),
        )
