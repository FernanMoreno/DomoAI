"""Small JSON repositories backed by the local SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from domoai.domain.automation import (
    AutomationConsent,
    AutomationRule,
    AutomationRuleStatus,
)
from domoai.domain.errors import DomainError, ErrorCode, InvalidTransitionError
from domoai.domain.household_consent import ConsentControlVersion
from domoai.domain.models import (
    AuditEvent,
    AuthorityContext,
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
    PlanExecutionLease,
    PlanStatus,
    RecurrenceRule,
    SourceCursor,
    SourceOrderingPolicy,
    StateSnapshot,
)
from domoai.domain.transitions import assert_plan_transition
from domoai.persistence.contract_compatibility import (
    compatibility_diagnostics,
    read_active_plan,
    read_active_rule,
)
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.runtime.approval_store import (
    ApprovalGrant,
    ConsentDecisionRecord,
    ConsentIssuanceReceipt,
    ConsentRevocationReceipt,
    DeliveryPermit,
    DeliveryReceipt,
)
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.events import CRITICAL_AUDIT_EVENT_TYPES, redact_payload
from domoai.runtime.state_store import StateStoreMetadata

_TABLES = {"devices", "policies", "plans"}


def _audit_event_from_outbox_row(row: Sequence[Any]) -> AuditEvent:
    return AuditEvent(
        id=row[0],
        event_type=row[1],
        actor=row[2],
        subject_id=row[3],
        payload=json.loads(row[4]),
        created_at=datetime.fromisoformat(row[5]),
        authority=AuthorityContext.model_validate(json.loads(row[6] or "{}")),
    )


class RuntimeOwnershipConflict(RuntimeError):
    """Another gateway owns, or uncertainly owned, this deployment."""


class RuntimeOwnershipRecoveryError(RuntimeError):
    """Sanitized failure while recovering a stale runtime owner."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RuntimeOwnershipRepository:
    """Durable singleton ownership for one physical gateway deployment."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def acquire(
        self,
        *,
        deployment_id: str,
        owner_id: str,
        config_digest: str,
        uncertain: bool = False,
    ) -> None:
        connection = self.database.connection
        acquired_at = self.clock.now().isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT owner_id, status, uncertain
                   FROM runtime_ownership WHERE deployment_id = ?""",
                (deployment_id,),
            ).fetchone()
            if existing is not None and (existing[1] != "released" or bool(existing[2])):
                reason = "uncertain" if bool(existing[2]) else "active"
                raise RuntimeOwnershipConflict(f"runtime ownership for {deployment_id} is {reason}")
            connection.execute(
                """INSERT INTO runtime_ownership
                   (deployment_id, owner_id, config_digest, acquired_at, released_at,
                    status, uncertain)
                   VALUES (?, ?, ?, ?, NULL, 'active', ?)
                   ON CONFLICT(deployment_id) DO UPDATE SET
                   owner_id=excluded.owner_id,
                   config_digest=excluded.config_digest,
                   acquired_at=excluded.acquired_at,
                   released_at=NULL,
                   status='active',
                   uncertain=excluded.uncertain""",
                (deployment_id, owner_id, config_digest, acquired_at, int(uncertain)),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def release(self, *, deployment_id: str, owner_id: str, uncertain: bool = False) -> bool:
        connection = self.database.connection
        cursor = connection.execute(
            """UPDATE runtime_ownership
               SET released_at = ?, status = ?, uncertain = ?
               WHERE deployment_id = ? AND owner_id = ? AND status = 'active'""",
            (
                self.clock.now().isoformat(),
                "blocked" if uncertain else "released",
                int(uncertain),
                deployment_id,
                owner_id,
            ),
        )
        connection.commit()
        return cursor.rowcount > 0

    async def release_stale(self, *, deployment_id: str, owner_id: str) -> bool:
        """Release an owner only after an administrator has acquired the DB lock.

        The caller must hold :meth:`SQLiteDatabase.advisory_lock` before
        invoking this method.  Comparing the recorded owner ID prevents a
        delayed recovery command from releasing a newer runtime lease.
        """

        connection = self.database.connection
        row = connection.execute(
            """SELECT owner_id, status, uncertain
               FROM runtime_ownership WHERE deployment_id = ?""",
            (deployment_id,),
        ).fetchone()
        if row is None:
            raise RuntimeOwnershipRecoveryError("runtime_owner_not_found")
        if row[0] != owner_id:
            raise RuntimeOwnershipRecoveryError("runtime_owner_mismatch")
        if row[1] == "released" and not bool(row[2]):
            return False
        if row[1] not in {"active", "blocked"}:
            raise RuntimeOwnershipRecoveryError("runtime_owner_invalid")
        cursor = connection.execute(
            """UPDATE runtime_ownership
               SET released_at = ?, status = 'released', uncertain = 0
               WHERE deployment_id = ? AND owner_id = ?
                 AND status IN ('active', 'blocked')""",
            (self.clock.now().isoformat(), deployment_id, owner_id),
        )
        connection.commit()
        if cursor.rowcount != 1:
            raise RuntimeOwnershipRecoveryError("runtime_owner_changed")
        return True


class ApprovalGrantRepository:
    """Durable consent control and atomic one-shot consumption.

    Mutators lock scope heads, all grants, then all reservations. PostgreSQL
    READ COMMITTED rereads each set under row locks; SQLite reserves its writer.
    """

    _DATETIME_FIELDS = {
        "issued_at",
        "approved_at",
        "expires_at",
        "validation_valid_until",
    }

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    @property
    def connection(self) -> Any:
        """Expose the transaction boundary to the outer storage service."""
        return self.database.connection

    @staticmethod
    def _read_delivery(row: Any) -> DeliveryReceipt:
        from domoai.runtime.approval_store import DeliveryReceipt, delivery_digest

        receipt = DeliveryReceipt.model_validate_json(row[7])
        permit = receipt.permit
        if (
            tuple(row[:7])
            != (
                permit.permit_id,
                permit.approval_id,
                permit.tenant_id,
                permit.household_id,
                permit.request_id,
                permit.idempotency_key,
                permit.request_digest,
            )
            or receipt.request_id != permit.request_id
            or delivery_digest(permit.model_dump(mode="json", exclude={"integrity"}))
            != permit.integrity
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery evidence is inconsistent")
        return receipt

    def get_delivery_receipt_sync(
        self, *, request_id: str, household_id: str, tenant_id: str | None = None
    ) -> DeliveryReceipt | None:
        query = "SELECT * FROM consent_deliveries WHERE household_id=? AND request_id=?"
        params = [household_id, request_id]
        if tenant_id is not None:
            query += " AND tenant_id=?"
            params.append(tenant_id)
        rows = self.database.connection.execute(query, params).fetchall()
        if len(rows) > 1:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery receipt requires tenant scope")
        return self._read_delivery(rows[0]) if rows else None

    def list_delivery_permits_sync(
        self, *, tenant_id: str, household_id: str
    ) -> list[DeliveryPermit]:
        rows = self.database.connection.execute(
            "SELECT * FROM consent_deliveries WHERE tenant_id=? AND household_id=? "
            "ORDER BY permit_id",
            self._scope(tenant_id, household_id),
        ).fetchall()
        return [self._read_delivery(row).permit for row in rows]

    def verify_delivery_permit_sync(self, permit: DeliveryPermit, *, command: Command) -> bool:
        from domoai.runtime.approval_store import delivery_digest

        row = self.database.connection.execute(
            "SELECT * FROM consent_deliveries WHERE permit_id=?",
            (permit.permit_id,),
        ).fetchone()
        if (
            row is None
            or self._read_delivery(row).permit != permit
            or delivery_digest(command.model_dump(mode="json")) != permit.command_digest
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery permit binding changed")
        return True

    def prepare_delivery_sync(
        self, connection: Any, *, preference_snapshot: Any = None, **request: Any
    ) -> DeliveryReceipt:
        """Prepare on exactly the caller connection; never own transaction lifecycle here."""
        from domoai.domain.coordination import (
            FencingToken,
            PhysicalIntent,
            PhysicalIntentClaimDecision,
            PhysicalIntentStatus,
        )
        from domoai.domain.household_preferences import PreferenceVersion
        from domoai.persistence.coordination import PhysicalIntentRepository
        from domoai.runtime.approval_store import (
            DeliveryPermit,
            DeliveryPreparation,
            DeliveryReceipt,
            delivery_digest,
            delivery_plan_binding,
        )

        try:
            data = DeliveryPreparation.model_validate(request)
            # model_copy bypasses nested model validators; revalidate the actual input bytes.
            plan = Plan.model_validate(data.plan.model_dump(mode="python"))
            intent = PhysicalIntent.model_validate(data.intent.model_dump(mode="python"))
            lease = FencingToken.model_validate(data.lease.model_dump(mode="python"))
            observed = data.protocol_limits.clock_observed_at
            if not callable(preference_snapshot) or observed.utcoffset() is None:
                raise ValueError("Missing transactional snapshot or clock bound")
            uncertainty = timedelta(seconds=data.protocol_limits.clock_uncertainty_seconds)
            max_end = (
                min(
                    data.now + timedelta(seconds=data.protocol_limits.d_max_seconds),
                    lease.expires_at,
                )
                - uncertainty
            )
        except (ValidationError, ValueError, TypeError, OverflowError) as error:
            raise DomainError(
                ErrorCode.DELIVERY_FENCE_UNAVAILABLE,
                "Delivery dependencies are invalid or unavailable",
            ) from error

        scope = self._scope(plan.authority.tenant_id, plan.authority.household_id)
        commands = [command for command in plan.commands if command.id == data.command_id]
        if len(commands) != 1:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery command is ambiguous")
        command = commands[0]
        command_digest = delivery_digest(command.model_dump(mode="json"))
        if (
            (intent.tenant_id, intent.household_id) != scope
            or (lease.scope.tenant_id, lease.scope.household_id) != scope
            or (intent.deployment_id, intent.owner_id, intent.fencing_epoch, intent.lease_id)
            != (lease.scope.deployment_id, lease.owner_id, lease.epoch, lease.lease_id)
            or (intent.plan_id, intent.command_id, intent.idempotency_key)
            != (plan.id, command.id, command.idempotency_key)
            or not intent.execution_attempt_id
            or not intent.owner_id
            or not intent.lease_id
            or intent.status is not PhysicalIntentStatus.PREPARED
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery intent scope changed")

        # T003 order: consent head, preference head, snapshot, grant, reservation, intent.
        head = connection.execute(
            "SELECT generation, revision FROM consent_control_heads "
            "WHERE tenant_id=? AND household_id=?" + self._for_update,
            scope,
        ).fetchone()
        preference_head = connection.execute(
            "SELECT generation, revision FROM household_preference_heads "
            "WHERE tenant_id=? AND household_id=?" + self._for_update,
            scope,
        ).fetchone()
        if head is None or preference_head is None:
            raise DomainError(ErrorCode.DELIVERY_FENCE_UNAVAILABLE, "Delivery head unavailable")
        current = ConsentControlVersion(generation=head[0], revision=head[1])
        preference_version, slots = preference_snapshot(connection)
        if preference_version != PreferenceVersion(
            generation=preference_head[0], revision=preference_head[1]
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Snapshot does not match locked scope")
        row = connection.execute(
            "SELECT payload, status, valid_until, consumed_at FROM approval_grants "
            "WHERE approval_id=?" + self._for_update,
            (data.approval_id,),
        ).fetchone()
        reservation = connection.execute(
            "SELECT reservation_id, status FROM approval_reservations WHERE approval_id=?"
            + self._for_update,
            (data.approval_id,),
        ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Unknown delivery approval")
        grant = json.loads(row[0])
        issuance = connection.execute(
            "SELECT tenant_id, household_id, generation FROM consent_issuance_receipts "
            "WHERE approval_id=?",
            (data.approval_id,),
        ).fetchone()
        revoked = connection.execute(
            "SELECT 1 FROM consent_revocations WHERE approval_id=?",
            (data.approval_id,),
        ).fetchone()
        if revoked is not None or row[1] == "revoked":
            raise DomainError(ErrorCode.CONSENT_REVOKED, "Consent has been revoked")
        if issuance is None or tuple(issuance) != (*scope, str(current.generation)):
            raise DomainError(ErrorCode.DELIVERY_FENCE_UNAVAILABLE, "Issuance scope unavailable")
        if (
            (
                grant.get("authority", {}).get("tenant_id"),
                grant.get("authority", {}).get("household_id"),
            )
            != scope
            or grant.get("plan_id") != plan.id
            or plan.validation is None
            or grant.get("validation_digest") != plan.validation.digest
            or grant.get("command_digests", {}).get(command.id) != command_digest
            or grant.get("validation_valid_until")
            != (plan.validation.valid_until.isoformat() if plan.validation.valid_until else None)
            or grant.get("window_digest")
            != (plan.execution_window.digest if plan.execution_window else None)
            or grant.get("schedule_revision", 0) != plan.schedule_revision
            or grant.get("bundle_digest") is not None
            or grant.get("recurrence_digest") is not None
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery approval binding changed")
        fingerprint = delivery_digest(data.model_dump(mode="json", exclude={"now"}))
        previous = connection.execute(
            "SELECT * FROM consent_deliveries "
            "WHERE tenant_id=? AND household_id=? AND request_id=?",
            (*scope, data.request_id),
        ).fetchone()
        if previous is not None:
            receipt = self._read_delivery(previous)
            if receipt.permit.request_digest != fingerprint:
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery request identity changed")
            stored_row = connection.execute(
                "SELECT payload, status, owner_id, execution_attempt_id, fencing_epoch, "
                "created_at, updated_at FROM physical_intents "
                "WHERE household_id=? AND idempotency_key=?" + self._for_update,
                (intent.household_id, intent.idempotency_key),
            ).fetchone()
            try:
                if stored_row is None:
                    raise ValueError("Delivery intent is missing")
                stored = PhysicalIntentRepository._read_consistent(
                    stored_row,
                    household_id=intent.household_id,
                    idempotency_key=intent.idempotency_key,
                )
                if (
                    stored.model_dump(exclude={"status", "updated_at"})
                    != intent.model_dump(exclude={"status", "updated_at"})
                    or row[1] != "consumed"
                    or row[3] is None
                    or (
                        data.reservation_id is not None
                        and (
                            reservation is None
                            or tuple(reservation) != (data.reservation_id, "committed")
                        )
                    )
                ):
                    raise ValueError("Delivery authority evidence changed")
            except ValueError as error:
                raise DomainError(
                    ErrorCode.CONSENT_CONFLICT, "Delivery intent evidence changed"
                ) from error
            return receipt
        # Historical retry above only returns existing evidence. A new preparation
        # must match the Plan constraints that were actually approved, including None.
        if (
            grant.get("plan_delivery_binding") != delivery_plan_binding(plan)
            or (plan.deployment_id is not None and plan.deployment_id != intent.deployment_id)
            or (
                plan.definition_digest is not None
                and plan.definition_digest != data.definition_digest
            )
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Approved Plan delivery binding changed")
        if (
            current != data.expected_control_version
            or preference_version != data.expected_preference_version
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery control version changed")
        if (
            observed > data.now
            or lease.issued_at > data.now
            or lease.expires_at <= data.now
            or data.not_before < data.now - uncertainty
            or data.not_before >= max_end
        ):
            raise DomainError(
                ErrorCode.DELIVERY_FENCE_UNAVAILABLE, "Delivery clock or window unavailable"
            )
        if (
            row[1] != "pending"
            or row[3] is not None
            or (row[2] is not None and datetime.fromisoformat(row[2]) <= data.now)
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery approval is not pending")
        if data.reservation_id is None:
            if reservation is not None and reservation[1] != "released":
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery approval is reserved")
        elif reservation is None or tuple(reservation) != (data.reservation_id, "reserved"):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery reservation changed")
        boundaries = [max_end]
        if plan.expires_at is not None:
            boundaries.append(plan.expires_at - uncertainty)
        for value in (row[2], grant.get("validation_valid_until")):
            if value is not None:
                boundaries.append(datetime.fromisoformat(value) - uncertainty)
        if plan.execution_window is not None:
            boundaries.append(plan.execution_window.not_after - uncertainty)
            if data.not_before < plan.execution_window.not_before:
                raise DomainError(
                    ErrorCode.DELIVERY_FENCE_UNAVAILABLE, "Delivery precedes the execution window"
                )
        for slot in slots:
            preference = slot.preference
            if slot.deleted or slot.erased or preference is None:
                continue
            # Without device/alias resolution this storage boundary cannot prove area/alias
            # restrictions irrelevant. Fail closed when active; bound future transitions.
            if slot.target.kind == "device" and slot.target.id not in {None, command.device_id}:
                continue
            if preference.valid_until is not None and preference.valid_until <= data.now:
                continue
            if preference.valid_from <= data.now:
                raise DomainError(
                    ErrorCode.DELIVERY_FENCE_UNAVAILABLE,
                    "Active preference requires admission qualification",
                )
            boundaries.append(preference.valid_from - uncertainty)
        not_after = min(boundaries)
        if not_after <= max(data.now, data.not_before):
            raise DomainError(ErrorCode.DELIVERY_FENCE_UNAVAILABLE, "Delivery window is empty")
        cursor = connection.execute(
            "UPDATE approval_grants SET status='consumed', consumed_at=? "
            "WHERE approval_id=? AND status='pending' AND consumed_at IS NULL",
            (data.now.isoformat(), data.approval_id),
        )
        if cursor.rowcount != 1:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery approval changed")
        if data.reservation_id is not None:
            cursor = connection.execute(
                "UPDATE approval_reservations SET status='committed', updated_at=? "
                "WHERE approval_id=? AND reservation_id=? AND status='reserved'",
                (data.now.isoformat(), data.approval_id, data.reservation_id),
            )
            if cursor.rowcount != 1:
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery reservation changed")
        claim = PhysicalIntentRepository.claim_for_dispatch_sync(connection, intent)
        if claim.decision is not PhysicalIntentClaimDecision.ACQUIRED:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery intent already exists")
        stored_row = connection.execute(
            "SELECT payload, status, owner_id, execution_attempt_id, fencing_epoch, "
            "created_at, updated_at FROM physical_intents "
            "WHERE household_id=? AND idempotency_key=?" + self._for_update,
            (intent.household_id, intent.idempotency_key),
        ).fetchone()
        try:
            if stored_row is None:
                raise ValueError("Prepared intent is missing")
            stored = PhysicalIntentRepository._read_consistent(
                stored_row,
                household_id=intent.household_id,
                idempotency_key=intent.idempotency_key,
            )
            if stored != claim.intent:
                raise ValueError("Prepared intent differs from its dispatching claim")
        except ValueError as error:
            raise DomainError(
                ErrorCode.CONSENT_CONFLICT, "Prepared intent evidence changed"
            ) from error
        permit = DeliveryPermit(
            permit_id=str(uuid4()),
            approval_id=data.approval_id,
            tenant_id=scope[0],
            household_id=scope[1],
            deployment_id=intent.deployment_id,
            control_version=current,
            preference_version=preference_version,
            generation=current.generation,
            plan_id=plan.id,
            command_id=command.id,
            idempotency_key=command.idempotency_key,
            execution_attempt_id=intent.execution_attempt_id,
            owner_id=intent.owner_id,
            fencing_epoch=intent.fencing_epoch,
            lease_id=intent.lease_id,
            receiver_id=data.receiver_id,
            route_id=data.route_id,
            receiver_generation=data.receiver_generation,
            definition_digest=data.definition_digest,
            command_digest=command_digest,
            request_id=data.request_id,
            request_digest=fingerprint,
            protocol_version=data.protocol_limits.version,
            issued_at=data.now,
            not_before=data.not_before,
            not_after=not_after,
            integrity="",
        )
        permit = permit.model_copy(
            update={
                "integrity": delivery_digest(permit.model_dump(mode="json", exclude={"integrity"}))
            }
        )
        receipt = DeliveryReceipt(request_id=data.request_id, permit=permit)
        cursor = connection.execute(
            "INSERT INTO consent_deliveries (permit_id, approval_id, tenant_id, household_id, "
            "request_id, idempotency_key, fingerprint, response) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                permit.permit_id,
                data.approval_id,
                *scope,
                data.request_id,
                command.idempotency_key,
                fingerprint,
                receipt.model_dump_json(),
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Delivery receipt was not persisted")
        return receipt

    @property
    def _for_update(self) -> str:
        from domoai.persistence.postgres import PostgresDatabase

        return " FOR UPDATE" if isinstance(self.database, PostgresDatabase) else ""

    @staticmethod
    def _scope(tenant_id: str, household_id: str) -> tuple[str, str]:
        if any(
            not isinstance(value, str) or not value.strip() or len(value) > 200
            for value in (tenant_id, household_id)
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Invalid consent scope")
        return tenant_id, household_id

    def _bootstrap_scope(self, tenant_id: str, household_id: str) -> None:
        connection = self.database.connection
        scope = self._scope(tenant_id, household_id)
        existing = connection.execute(
            "SELECT 1 FROM consent_control_heads WHERE tenant_id=? AND household_id=?", scope
        ).fetchone()
        if existing is None:
            history = connection.execute(
                """SELECT 1 FROM approval_grants
                   WHERE json_extract(payload, '$.authority.tenant_id')=?
                     AND json_extract(payload, '$.authority.household_id')=? LIMIT 1""",
                scope,
            ).fetchone()
            if history is not None:
                raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Consent head is unavailable")
        connection.execute(
            """INSERT INTO consent_control_heads
               (tenant_id, household_id, generation, revision) VALUES (?, ?, ?, 0)
               ON CONFLICT(tenant_id, household_id) DO NOTHING""",
            (*scope, str(uuid4())),
        )

    def bootstrap_consent_sync(self, *, tenant_id: str, household_id: str) -> None:
        """Provision an explicitly selected new scope without issuing any grant."""
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._bootstrap_scope(tenant_id, household_id)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def get_consent_control_sync(
        self, *, tenant_id: str, household_id: str
    ) -> ConsentControlVersion:
        row = self.database.connection.execute(
            "SELECT generation, revision FROM consent_control_heads "
            "WHERE tenant_id=? AND household_id=?",
            self._scope(tenant_id, household_id),
        ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Consent head is unavailable")
        return ConsentControlVersion(generation=row[0], revision=row[1])

    def _lock_scope(self, scope: tuple[str, str]) -> ConsentControlVersion:
        row = self.database.connection.execute(
            "SELECT generation, revision FROM consent_control_heads "
            "WHERE tenant_id=? AND household_id=?" + self._for_update,
            scope,
        ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Consent head is unavailable")
        return ConsentControlVersion(generation=row[0], revision=row[1])

    def _lock_grants(self, approval_ids: list[str]) -> dict[str, tuple[Any, ...]]:
        """Discover immutable scopes, then reread every grant/reservation under locks."""
        ids = sorted(set(approval_ids))
        placeholders = ",".join("?" for _ in ids)
        connection = self.database.connection
        discovered = connection.execute(
            "SELECT approval_id, payload FROM approval_grants "
            f"WHERE approval_id IN ({placeholders})",
            ids,
        ).fetchall()
        if len(discovered) != len(ids):
            return {}
        scopes = {
            self._scope(
                json.loads(row[1]).get("authority", {}).get("tenant_id"),
                json.loads(row[1]).get("authority", {}).get("household_id"),
            )
            for row in discovered
        }
        if len(scopes) != 1:
            return {}
        scope = next(iter(scopes))
        self._lock_scope(scope)
        rows = connection.execute(
            f"SELECT approval_id, payload, status, valid_until, consumed_at "
            f"FROM approval_grants WHERE approval_id IN ({placeholders}) "
            "ORDER BY approval_id" + self._for_update,
            ids,
        ).fetchall()
        for row in rows:
            authority = json.loads(row[1]).get("authority", {})
            if (authority.get("tenant_id"), authority.get("household_id")) != scope:
                return {}
        connection.execute(
            f"SELECT approval_id FROM approval_reservations "
            f"WHERE approval_id IN ({placeholders}) ORDER BY approval_id" + self._for_update,
            ids,
        ).fetchall()
        return {row[0]: tuple(row) for row in rows}

    def assert_consent_active_sync(self, approval_id: str) -> None:
        connection = self.database.connection
        row = connection.execute(
            """SELECT grants.status, heads.generation, receipts.generation,
                      revocations.approval_id
               FROM approval_grants AS grants
               LEFT JOIN consent_issuance_receipts AS receipts
                 ON receipts.approval_id=grants.approval_id
                 AND receipts.tenant_id=json_extract(grants.payload, '$.authority.tenant_id')
                 AND receipts.household_id=json_extract(grants.payload, '$.authority.household_id')
               LEFT JOIN consent_control_heads AS heads
                 ON heads.tenant_id=receipts.tenant_id AND heads.household_id=receipts.household_id
               LEFT JOIN consent_revocations AS revocations
                 ON revocations.approval_id=grants.approval_id
               WHERE grants.approval_id=?""",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Unknown approval")
        if row[0] == "revoked" or row[3] is not None:
            raise DomainError(ErrorCode.CONSENT_REVOKED, "Consent has been revoked")
        if row[0] not in {"pending", "consumed"}:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Approval is not active")
        if row[1] is None or row[1] != row[2]:
            raise DomainError(ErrorCode.SOURCE_UNAVAILABLE, "Consent generation is unavailable")

    def _active(self, approval_id: str) -> bool:
        try:
            self.assert_consent_active_sync(approval_id)
        except DomainError:
            return False
        return True

    async def save(self, grant: ApprovalGrant) -> None:
        self.save_sync(grant)

    def save_sync(
        self, grant: ApprovalGrant, *, decision: ConsentDecisionRecord | None = None
    ) -> None:
        if decision is not None and (
            decision.principal_id != grant.approved_by
            or any(
                not value.strip() or len(value) > 200
                for value in (decision.principal_id, decision.request_id)
            )
        ):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Invalid consent request identity")
        payload = asdict(grant)
        payload["authority"] = grant.authority.model_dump(mode="json")
        serialized = json.dumps(
            payload,
            sort_keys=True,
            default=lambda value: value.isoformat() if isinstance(value, datetime) else value,
        )
        connection = self.database.connection
        scope = self._scope(grant.authority.tenant_id, grant.authority.household_id)
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._bootstrap_scope(*scope)
            version = self._lock_scope(scope)
            cursor = connection.execute(
                """INSERT INTO approval_grants
                   (approval_id, payload, issued_at, valid_until, status)
                   VALUES (?, ?, ?, ?, 'pending') ON CONFLICT DO NOTHING""",
                (
                    grant.approval_id,
                    serialized,
                    grant.issued_at.isoformat(),
                    grant.expires_at.isoformat() if grant.expires_at is not None else None,
                ),
            )
            if cursor.rowcount != 1:
                if grant.assertion_nonce is not None and self.nonce_exists_sync(
                    grant.assertion_nonce
                ):
                    raise DomainError(
                        ErrorCode.APPROVAL_ASSERTION_REPLAYED,
                        "Approval assertion nonce has already been used",
                    )
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Approval identity already exists")
            connection.execute(
                """INSERT INTO consent_issuance_receipts
                   (approval_id, assertion_nonce, tenant_id, household_id, generation,
                    revision, receipt_id, issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    grant.approval_id,
                    grant.assertion_nonce,
                    *scope,
                    str(version.generation),
                    version.revision,
                    str(uuid4()),
                    grant.issued_at.isoformat(),
                ),
            )
            if decision is not None:
                if grant.expires_at is None:
                    raise DomainError(ErrorCode.CONSENT_CONFLICT, "Approval has no expiry")
                response = json.dumps(
                    {
                        "kind": "approval",
                        "approval_id": grant.approval_id,
                        "expires_at": grant.expires_at.astimezone(UTC)
                        .isoformat()
                        .replace("+00:00", "Z"),
                        "review_digest": decision.review_digest,
                        "reviewed_control_version": decision.reviewed_control_version,
                    },
                    sort_keys=True,
                )
                stored = connection.execute(
                    """INSERT INTO consent_receipts
                       (tenant_id, household_id, principal_id, request_id, fingerprint, response)
                       VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                    (
                        *scope,
                        decision.principal_id,
                        decision.request_id,
                        decision.fingerprint,
                        response,
                    ),
                )
                if stored.rowcount != 1:
                    raise DomainError(
                        ErrorCode.CONSENT_CONFLICT, "Consent request identity conflict"
                    )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def get_decision_receipt_sync(
        self, *, tenant_id: str, household_id: str, principal_id: str, request_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        """Approval receipt for one principal's request id; revocation rows are never returned."""
        row = self.database.connection.execute(
            """SELECT fingerprint, response FROM consent_receipts
               WHERE tenant_id=? AND household_id=? AND principal_id=? AND request_id=?""",
            (tenant_id, household_id, principal_id, request_id),
        ).fetchone()
        if row is None:
            return None
        response = json.loads(row[1])
        if response.get("kind") != "approval":
            return None
        return str(row[0]), response

    async def get(self, approval_id: str) -> ApprovalGrant | None:
        return self.get_sync(approval_id)

    def get_sync(self, approval_id: str) -> ApprovalGrant | None:
        row = self.database.connection.execute(
            "SELECT payload FROM approval_grants WHERE approval_id=?", (approval_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["authority"] = AuthorityContext.model_validate(payload.get("authority", {}))
        for field_name in self._DATETIME_FIELDS:
            if payload.get(field_name) is not None:
                payload[field_name] = datetime.fromisoformat(payload[field_name])
        return ApprovalGrant(**payload)

    def get_issuance_receipt_sync(self, *, assertion_nonce: str) -> ConsentIssuanceReceipt | None:
        row = self.database.connection.execute(
            """SELECT approval_id, assertion_nonce, tenant_id, household_id, generation,
                      revision, receipt_id, issued_at FROM consent_issuance_receipts
               WHERE assertion_nonce=?""",
            (assertion_nonce,),
        ).fetchone()
        if row is None:
            return None
        return ConsentIssuanceReceipt(
            approval_id=row[0],
            assertion_nonce=row[1],
            tenant_id=row[2],
            household_id=row[3],
            control_version=ConsentControlVersion(generation=row[4], revision=row[5]),
            receipt_id=row[6],
            issued_at=datetime.fromisoformat(row[7]),
        )

    @staticmethod
    def _read_revocation(response: str) -> ConsentRevocationReceipt:
        value = json.loads(response)
        for key in ("expected_version", "control_version"):
            value[key] = ConsentControlVersion.model_validate(value[key])
        value["revoked_at"] = datetime.fromisoformat(value["revoked_at"])
        return ConsentRevocationReceipt(**value)

    def get_consent_receipt_sync(
        self,
        *,
        tenant_id: str,
        household_id: str,
        request_id: str,
        principal_id: str | None = None,
    ) -> ConsentRevocationReceipt | None:
        sql = (
            "SELECT response FROM consent_receipts "
            "WHERE tenant_id=? AND household_id=? AND request_id=?"
        )
        parameters = [tenant_id, household_id, request_id]
        if principal_id is not None:
            sql += " AND principal_id=?"
            parameters.append(principal_id)
        rows = self.database.connection.execute(sql, parameters).fetchall()
        kinds = [json.loads(row[0]).get("kind") for row in rows]
        if principal_id is not None and "approval" in kinds:
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Consent request identity conflict")
        # Approval receipts are not revocations: they never count toward the ambiguity check.
        rows = [row for row, kind in zip(rows, kinds, strict=True) if kind != "approval"]
        if len(rows) > 1:
            raise DomainError(
                ErrorCode.CONSENT_CONFLICT, "Consent receipt requires principal scope"
            )
        return self._read_revocation(rows[0][0]) if rows else None

    def list_active_approvals_sync(
        self,
        tenant_id: str,
        household_id: str,
        plan_id: str,
        now: datetime,
        issued_not_after: datetime | None = None,
    ) -> list[tuple[str, str]]:
        """``(approval_id, status)`` of the household's unrevoked grants for one plan.

        Pending grants only count while ``valid_until`` is strictly in the future;
        consumed ones stay active until revoked. Each candidate is confirmed by
        ``_active`` so "active" has a single definition. ``issued_not_after`` drops
        grants issued after a given instant (what a reviewer could have seen).
        """
        scope = self._scope(tenant_id, household_id)
        bound = "" if issued_not_after is None else " AND julianday(grants.issued_at)<=julianday(?)"
        extra = () if issued_not_after is None else (issued_not_after.isoformat(),)
        rows = self.database.connection.execute(
            """SELECT grants.approval_id, grants.status
               FROM approval_grants AS grants
               LEFT JOIN consent_revocations AS revocations
                 ON revocations.approval_id=grants.approval_id
               WHERE revocations.approval_id IS NULL
                 AND grants.status IN ('pending', 'consumed')
                 AND json_extract(grants.payload, '$.authority.tenant_id')=?
                 AND json_extract(grants.payload, '$.authority.household_id')=?
                 AND json_extract(grants.payload, '$.plan_id')=?
                 AND (grants.status='consumed' OR grants.valid_until IS NULL
                      OR julianday(grants.valid_until)>julianday(?))"""
            + bound
            + " ORDER BY grants.approval_id",
            (*scope, plan_id, now.isoformat(), *extra),
        ).fetchall()
        return [(row[0], row[1]) for row in rows if self._active(row[0])]

    def list_active_approval_ids_sync(
        self,
        tenant_id: str,
        household_id: str,
        plan_id: str,
        now: datetime,
        issued_not_after: datetime | None = None,
    ) -> list[str]:
        return [
            approval_id
            for approval_id, _status in self.list_active_approvals_sync(
                tenant_id, household_id, plan_id, now, issued_not_after
            )
        ]

    def get_consent_revocation_sync(self, approval_id: str) -> ConsentRevocationReceipt | None:
        row = self.database.connection.execute(
            "SELECT response FROM consent_revocations WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return self._read_revocation(row[0]) if row else None

    def revoke_consent_sync(
        self,
        *,
        tenant_id: str,
        household_id: str,
        approval_id: str,
        expected_version: ConsentControlVersion,
        principal_id: str,
        request_id: str,
        now: datetime,
        request_fingerprint: str | None = None,
    ) -> ConsentRevocationReceipt:
        if now.utcoffset() is None:
            raise ValueError("consent revocation time must be timezone-aware")
        scope = self._scope(tenant_id, household_id)
        if any(not value.strip() or len(value) > 200 for value in (principal_id, request_id)):
            raise DomainError(ErrorCode.CONSENT_CONFLICT, "Invalid consent request identity")
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            grant = self.get_sync(approval_id)
            if grant is None or (grant.authority.tenant_id, grant.authority.household_id) != scope:
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Consent target is unavailable")
            rows = self._lock_grants([approval_id])
            if not rows:
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Consent target is unavailable")
            current = self.get_consent_control_sync(tenant_id=tenant_id, household_id=household_id)
            identity: list[Any] = [
                approval_id,
                rows[approval_id][1],
                expected_version.model_dump(mode="json"),
            ]
            if request_fingerprint is not None:
                # Resident revocations also bind the exact decision body; the legacy
                # (operator) formula is unchanged when no fingerprint is given.
                identity.append(request_fingerprint)
            fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            previous = connection.execute(
                """SELECT fingerprint, response FROM consent_receipts
                   WHERE tenant_id=? AND household_id=? AND principal_id=? AND request_id=?""",
                (*scope, principal_id, request_id),
            ).fetchone()
            if previous is not None:
                if previous[0] != fingerprint or json.loads(previous[1]).get("kind") == "approval":
                    raise DomainError(
                        ErrorCode.CONSENT_CONFLICT, "Consent request identity conflict"
                    )
                connection.commit()
                return self._read_revocation(previous[1])
            if (
                current != expected_version
                or self.get_consent_revocation_sync(approval_id) is not None
            ):
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Consent control changed")
            next_version = ConsentControlVersion(
                generation=current.generation, revision=current.revision + 1
            )
            deliveries = connection.execute(
                "SELECT * FROM consent_deliveries WHERE approval_id=? ORDER BY permit_id",
                (approval_id,),
            ).fetchall()
            permits = [self._read_delivery(row).permit for row in deliveries]
            receipt = ConsentRevocationReceipt(
                approval_id=approval_id,
                tenant_id=tenant_id,
                household_id=household_id,
                principal_id=principal_id,
                request_id=request_id,
                revocation_id=str(uuid4()),
                expected_version=current,
                control_version=next_version,
                revoked_at=now,
                permit_ids=[permit.permit_id for permit in permits],
                routes=sorted({permit.route_id for permit in permits}),
            )
            response = asdict(receipt)
            response["expected_version"] = current.model_dump(mode="json")
            response["control_version"] = next_version.model_dump(mode="json")
            serialized = json.dumps(response, sort_keys=True, default=str)
            cursor = connection.execute(
                """UPDATE consent_control_heads SET revision=?
                   WHERE tenant_id=? AND household_id=? AND generation=? AND revision=?""",
                (next_version.revision, *scope, str(current.generation), current.revision),
            )
            if cursor.rowcount != 1:
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Consent control changed")
            connection.execute(
                """INSERT INTO consent_revocations
                   (approval_id, tenant_id, household_id, revocation_id, generation, revision,
                    principal_id, request_id, revoked_at, response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    approval_id,
                    *scope,
                    receipt.revocation_id,
                    str(current.generation),
                    next_version.revision,
                    principal_id,
                    request_id,
                    now.isoformat(),
                    serialized,
                ),
            )
            stored = connection.execute(
                """INSERT INTO consent_receipts
                   (tenant_id, household_id, principal_id, request_id, fingerprint, response)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
                (*scope, principal_id, request_id, fingerprint, serialized),
            )
            if stored.rowcount != 1:
                raise DomainError(ErrorCode.CONSENT_CONFLICT, "Consent request identity conflict")
            connection.commit()
            return receipt
        except BaseException:
            connection.rollback()
            raise

    async def consume_if_pending(self, approval_id: str, *, now: datetime) -> bool:
        return self.consume_if_pending_sync(approval_id, now=now)

    def consume_if_pending_sync(self, approval_id: str, *, now: datetime) -> bool:
        if now.utcoffset() is None:
            raise ValueError("approval consumption time must be timezone-aware")
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._lock_grants([approval_id])
            if not rows or not self._active(approval_id):
                connection.rollback()
                return False
            reservation = self.reservation_status_sync(approval_id)
            if reservation is not None and reservation[1] != "released":
                connection.rollback()
                return False
            cursor = connection.execute(
                """UPDATE approval_grants SET status='consumed', consumed_at=?
                   WHERE approval_id=? AND status='pending'
                   AND (valid_until IS NULL OR julianday(valid_until)>julianday(?))""",
                (now.isoformat(), approval_id, now.isoformat()),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise

    def is_pending_sync(self, approval_id: str) -> bool:
        row = self.database.connection.execute(
            "SELECT status FROM approval_grants WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return row is not None and row[0] == "pending" and self._active(approval_id)

    def is_consumed_sync(self, approval_id: str) -> bool:
        row = self.database.connection.execute(
            "SELECT status FROM approval_grants WHERE approval_id=?", (approval_id,)
        ).fetchone()
        return row is not None and row[0] == "consumed"

    def reservation_status_sync(self, approval_id: str) -> tuple[str, str] | None:
        row = self.database.connection.execute(
            "SELECT reservation_id, status FROM approval_reservations WHERE approval_id=?",
            (approval_id,),
        ).fetchone()
        return None if row is None else (str(row[0]), str(row[1]))

    def list_reservation_approval_ids_sync(self, reservation_id: str) -> list[str]:
        rows = self.database.connection.execute(
            "SELECT approval_id FROM approval_reservations "
            "WHERE reservation_id=? ORDER BY approval_id",
            (reservation_id,),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def reserve_if_pending_sync(
        self, approval_id: str, *, reservation_id: str, now: datetime
    ) -> bool:
        if now.utcoffset() is None:
            raise ValueError("approval reservation time must be timezone-aware")
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._lock_grants([approval_id])
            if not rows or not self._active(approval_id):
                connection.rollback()
                return False
            row = rows[approval_id]
            if row[2] != "pending" or (
                row[3] is not None and datetime.fromisoformat(row[3]) <= now
            ):
                connection.rollback()
                return False
            existing = self.reservation_status_sync(approval_id)
            if existing is not None and existing[1] != "released":
                connection.rollback()
                return existing == (reservation_id, "reserved")
            if existing is None:
                cursor = connection.execute(
                    """INSERT INTO approval_reservations
                       (approval_id, reservation_id, status, created_at, updated_at)
                       VALUES (?, ?, 'reserved', ?, ?)""",
                    (approval_id, reservation_id, now.isoformat(), now.isoformat()),
                )
            else:
                cursor = connection.execute(
                    """UPDATE approval_reservations
                       SET reservation_id=?, status='reserved', updated_at=?
                       WHERE approval_id=? AND status='released'""",
                    (reservation_id, now.isoformat(), approval_id),
                )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise

    def commit_reservation_sync(
        self, approval_id: str, *, reservation_id: str, now: datetime
    ) -> bool:
        return self.commit_reservation_batch_sync(
            [approval_id], reservation_id=reservation_id, now=now
        )

    def commit_reservation_batch_sync(
        self, approval_ids: list[str], *, reservation_id: str, now: datetime
    ) -> bool:
        if now.utcoffset() is None:
            raise ValueError("approval commit time must be timezone-aware")
        ids = sorted(set(approval_ids))
        if not ids:
            return True
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = self._lock_grants(ids)
            if not rows or any(not self._active(approval_id) for approval_id in ids):
                connection.rollback()
                return False
            pending = []
            for approval_id in ids:
                reservation = self.reservation_status_sync(approval_id)
                if reservation is None or reservation[0] != reservation_id:
                    connection.rollback()
                    return False
                row = rows[approval_id]
                if reservation[1] == "committed" and row[2] == "consumed":
                    continue
                if (
                    reservation[1] != "reserved"
                    or row[2] != "pending"
                    or (row[3] is not None and datetime.fromisoformat(row[3]) <= now)
                ):
                    connection.rollback()
                    return False
                pending.append(approval_id)
            for approval_id in pending:
                grant_cursor = connection.execute(
                    """UPDATE approval_grants SET status='consumed', consumed_at=?
                       WHERE approval_id=? AND status='pending'
                       AND (valid_until IS NULL OR julianday(valid_until)>julianday(?))""",
                    (now.isoformat(), approval_id, now.isoformat()),
                )
                reservation_cursor = connection.execute(
                    """UPDATE approval_reservations SET status='committed', updated_at=?
                       WHERE approval_id=? AND reservation_id=? AND status='reserved'""",
                    (now.isoformat(), approval_id, reservation_id),
                )
                if grant_cursor.rowcount != 1 or reservation_cursor.rowcount != 1:
                    connection.rollback()
                    return False
            connection.commit()
            return True
        except BaseException:
            connection.rollback()
            raise

    def release_reservation_sync(
        self, approval_id: str, *, reservation_id: str, now: datetime
    ) -> bool:
        if now.utcoffset() is None:
            raise ValueError("approval release time must be timezone-aware")
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            if not self._lock_grants([approval_id]):
                connection.rollback()
                return False
            cursor = connection.execute(
                """UPDATE approval_reservations SET status='released', updated_at=?
                   WHERE approval_id=? AND reservation_id=? AND status='reserved'""",
                (now.isoformat(), approval_id, reservation_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise

    def nonce_exists_sync(self, nonce: str) -> bool:
        row = self.database.connection.execute(
            "SELECT 1 FROM approval_grants "
            "WHERE json_extract(payload, '$.assertion_nonce')=? LIMIT 1",
            (nonce,),
        ).fetchone()
        return row is not None


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
        connection = self.database.connection
        connection.execute(
            """INSERT OR IGNORE INTO audit_events
               (id, event_type, actor, subject_id, payload, created_at, authority_payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.id,
                event.event_type,
                event.actor,
                event.subject_id,
                json.dumps(redact_payload(event.payload), sort_keys=True),
                event.created_at.isoformat(),
                json.dumps(event.authority.model_dump(mode="json"), sort_keys=True),
            ),
        )
        if event.event_type in CRITICAL_AUDIT_EVENT_TYPES:
            connection.execute(
                """INSERT OR IGNORE INTO audit_outbox
                   (event_id, event_type, actor, subject_id, payload, created_at, authority_payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event.id,
                    event.event_type,
                    event.actor,
                    event.subject_id,
                    json.dumps(redact_payload(event.payload), sort_keys=True),
                    event.created_at.isoformat(),
                    json.dumps(event.authority.model_dump(mode="json"), sort_keys=True),
                ),
            )
        connection.commit()

    async def list_pending_outbox(self, limit: int = 100) -> list[AuditEvent]:
        if limit < 1:
            raise ValueError("outbox limit must be at least 1")
        cursor = self.database.connection.execute(
            """SELECT event_id, event_type, actor, subject_id, payload, created_at
                      , authority_payload
               FROM audit_outbox WHERE status = 'pending'
               ORDER BY sequence LIMIT ?""",
            (limit,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [_audit_event_from_outbox_row(row) for row in rows]

    async def claim_pending_outbox(
        self,
        *,
        limit: int = 100,
        owner_id: str,
        lease_id: str,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> list[AuditEvent]:
        """Claim pending entries with a transactionally exclusive lease.

        SQLite obtains a writer lock with ``BEGIN IMMEDIATE``. PostgreSQL
        adapts that to ``BEGIN`` and the conditional updates serialize on each
        row. The update predicate is intentionally repeated after selection:
        a concurrent claimant can see the same candidate, but only one update
        can win the owner/lease CAS.
        """

        if limit < 1:
            raise ValueError("outbox limit must be at least 1")
        if not owner_id.strip() or not lease_id.strip():
            raise ValueError("outbox claim owner and lease ID must be non-empty")
        if lease_seconds <= 0:
            raise ValueError("outbox lease duration must be positive")
        claimed_at = now or datetime.now(UTC)
        if claimed_at.tzinfo is None:
            raise ValueError("outbox claim time must be timezone-aware")
        expires_at = (claimed_at + timedelta(seconds=lease_seconds)).isoformat()
        claimed_at_text = claimed_at.isoformat()
        connection = self.database.connection
        claimed: list[AuditEvent] = []
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """SELECT event_id, event_type, actor, subject_id, payload, created_at,
                          authority_payload
                   FROM audit_outbox
                   WHERE status = 'pending'
                     AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                   ORDER BY sequence LIMIT ?""",
                (claimed_at_text, limit),
            )
            rows = cursor.fetchall()
            cursor.close()
            for row in rows:
                update = connection.execute(
                    """UPDATE audit_outbox
                       SET lease_owner = ?, lease_id = ?, lease_expires_at = ?
                       WHERE event_id = ?
                         AND status = 'pending'
                         AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                    (owner_id, lease_id, expires_at, row[0], claimed_at_text),
                )
                if update.rowcount == 1:
                    claimed.append(_audit_event_from_outbox_row(row))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return claimed

    async def mark_outbox_delivered(
        self,
        event_id: str,
        *,
        owner_id: str,
        lease_id: str,
        delivered_at: datetime | None = None,
    ) -> bool:
        completed_at = delivered_at or datetime.now(UTC)
        cursor = self.database.connection.execute(
            """UPDATE audit_outbox
               SET status = 'delivered', delivered_at = ?,
                   lease_owner = NULL, lease_id = NULL, lease_expires_at = NULL
               WHERE event_id = ? AND status = 'pending'
                 AND lease_owner = ? AND lease_id = ?""",
            (completed_at.isoformat(), event_id, owner_id, lease_id),
        )
        self.database.connection.commit()
        return cursor.rowcount == 1

    async def record_outbox_retry(
        self,
        event_id: str,
        error: str,
        *,
        owner_id: str,
        lease_id: str,
    ) -> bool:
        cursor = self.database.connection.execute(
            """UPDATE audit_outbox
               SET attempts = attempts + 1, last_error = ?,
                   lease_owner = NULL, lease_id = NULL, lease_expires_at = NULL
               WHERE event_id = ? AND status = 'pending'
                 AND lease_owner = ? AND lease_id = ?""",
            (str(redact_payload(error))[:200], event_id, owner_id, lease_id),
        )
        self.database.connection.commit()
        return cursor.rowcount == 1

    async def list_all(self) -> list[AuditEvent]:
        cursor = self.database.connection.execute(
            """SELECT id, event_type, actor, subject_id, payload, created_at, authority_payload
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
                authority=AuthorityContext.model_validate(json.loads(row[6] or "{}")),
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
        if limit < 1:
            raise ValueError("audit event limit must be at least 1")
        bounded_limit = min(limit, self._MAX_LIST_EVENTS_LIMIT)
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
        params.append(bounded_limit)

        cursor = self.database.connection.execute(
            f"""SELECT id, event_type, actor, subject_id, payload, created_at, authority_payload
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
                authority=AuthorityContext.model_validate(json.loads(row[6] or "{}")),
            )
            for row in rows
        ]


class PlanRepository:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        clock: Clock | None = None,
        deployment_id: str | None = None,
    ) -> None:
        self._repository = SQLiteJsonRepository(database, "plans")
        self.clock = clock or SystemClock()
        self.deployment_id = deployment_id

    def _bind_deployment(self, plan: Plan) -> Plan:
        plan = Plan.model_validate(plan.model_dump(mode="python"))
        if plan.execution_lease is not None and (
            plan.deployment_id is not None
            and plan.execution_lease.deployment_id != plan.deployment_id
        ):
            raise DomainError(
                ErrorCode.PLAN_IDENTITY_CONFLICT,
                "Plan deployment and execution lease deployment do not match",
                details={"plan_id": plan.id},
            )
        if self.deployment_id is None:
            return plan
        if plan.deployment_id is not None and plan.deployment_id != self.deployment_id:
            raise DomainError(
                ErrorCode.PLAN_IDENTITY_CONFLICT,
                "Plan belongs to another deployment",
                details={"plan_id": plan.id},
            )
        if plan.execution_lease is not None and (
            plan.execution_lease.deployment_id != self.deployment_id
        ):
            raise DomainError(
                ErrorCode.PLAN_IDENTITY_CONFLICT,
                "Plan execution lease belongs to another deployment",
                details={"plan_id": plan.id},
            )
        if plan.deployment_id == self.deployment_id:
            return plan
        if (
            plan.status is not PlanStatus.DRAFT
            or plan.definition_digest is not None
            or plan.validation is not None
            or plan.approval is not None
        ):
            raise DomainError(
                ErrorCode.PLAN_IDENTITY_CONFLICT,
                "Plan requires revalidation as a new definition",
                details={"reason": "revalidation_required", "plan_id": plan.id},
            )
        payload = plan.model_dump(mode="python", exclude={"schema_version"})
        return Plan.model_validate({**payload, "deployment_id": self.deployment_id})

    async def compatibility_diagnostics(
        self,
        *,
        tenant_id: str,
        household_id: str,
        deployment_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return compatibility_diagnostics(
            self._repository.database.connection,
            record_kind="plan",
            tenant_id=tenant_id,
            household_id=household_id,
            deployment_id=deployment_id,
            limit=limit,
            offset=offset,
        )

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
        plan = self._bind_deployment(plan)
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
                stored_plan = read_active_plan(existing[1])
                if stored_plan.definition_digest is not None and (
                    stored_plan.definition_digest != plan.definition_digest
                    or stored_plan.schema_version != plan.schema_version
                    or stored_plan.deployment_id != plan.deployment_id
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
        row = self._repository.database.connection.execute(
            "SELECT payload FROM plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
        return read_active_plan(row[0], record_id=plan_id) if row is not None else None

    async def mark_unknown_if_executing(
        self,
        plan_id: str,
        *,
        expected_execution_lease: PlanExecutionLease | None = None,
        allow_legacy: bool = False,
    ) -> bool:
        """Atomically settle orphan evidence without making it claimable again."""

        connection = self._repository.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload FROM plans WHERE id = ? AND status = ?",
                (plan_id, PlanStatus.EXECUTING.value),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            payload = json.loads(row[0])
            current = Plan.model_validate(payload)
            if self.deployment_id is not None and current.deployment_id not in {
                self.deployment_id,
                None,
            }:
                connection.rollback()
                return False
            if (
                self.deployment_id is not None
                and current.deployment_id is None
                and not allow_legacy
            ):
                connection.rollback()
                return False
            if expected_execution_lease is not None and (
                current.execution_lease != expected_execution_lease
            ):
                connection.rollback()
                return False
            payload["status"] = PlanStatus.UNKNOWN.value
            cursor = connection.execute(
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
                connection.rollback()
                return False
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise

    async def claim_for_execution(
        self, plan: Plan, *, allowed_statuses: frozenset[PlanStatus]
    ) -> bool:
        plan = self._bind_deployment(plan)
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
                stored_plan = read_active_plan(existing[1])
                if stored_plan.definition_digest is not None and (
                    stored_plan.definition_digest != plan.definition_digest
                    or stored_plan.schema_version != plan.schema_version
                    or stored_plan.deployment_id != plan.deployment_id
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

    async def list_by_status(
        self,
        statuses: frozenset[PlanStatus],
        *,
        deployment_id: str | None = None,
        include_legacy: bool = False,
    ) -> list[Plan]:
        placeholders = ",".join("?" for _ in statuses)
        clauses = [f"status IN ({placeholders})"]
        params: list[object] = [status.value for status in statuses]
        if deployment_id is not None:
            deployment_clause = "json_extract(payload, '$.deployment_id') = ?"
            params.append(deployment_id)
            if include_legacy:
                deployment_clause = (
                    "(json_extract(payload, '$.deployment_id') = ? "
                    "OR json_extract(payload, '$.deployment_id') IS NULL)"
                )
            clauses.append(deployment_clause)
        cursor = self._repository.database.connection.execute(
            f"SELECT payload FROM plans WHERE {' AND '.join(clauses)}",
            params,
        )
        rows = cursor.fetchall()
        cursor.close()
        compatible = []
        for row in rows:
            try:
                compatible.append(read_active_plan(row[0]))
            except DomainError:
                continue
        return compatible


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
                BundleMemberCommitStatus.DEPENDENCY_FAILED,
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
        if statuses & {BundleMemberCommitStatus.FAILED, BundleMemberCommitStatus.DEPENDENCY_FAILED}:
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
                member.plan_id == plan_id and member.status is BundleMemberCommitStatus.SCHEDULED
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


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _in_clause(column: str, count: int) -> str:
    return f"{column} IN ({', '.join('?' for _ in range(count))})"


@dataclass(frozen=True)
class StateHistoryRecord:
    history_id: str
    snapshot: StateSnapshot


class StateHistoryRepository:
    """Household-scoped historical semantic state samples."""

    _MAX_LIST_LIMIT = 500

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        household_id: str = "default",
        retention_days: int = 90,
        clock: Clock | None = None,
    ) -> None:
        if not household_id.strip():
            raise ValueError("household_id must not be blank")
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        self.database = database
        self.household_id = household_id
        self.retention_days = retention_days
        self.clock = clock or SystemClock()

    async def append(self, snapshots: Sequence[StateSnapshot]) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self.append_without_commit(connection, snapshots)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    async def purge_expired(self) -> int:
        """Delete expired history even when the household has no new events."""

        connection = self.database.connection
        cutoff = self.clock.now() - timedelta(days=self.retention_days)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """DELETE FROM state_history
                   WHERE household_id = ? AND julianday(received_at) < julianday(?)""",
                (self.household_id, cutoff.astimezone(UTC).isoformat()),
            )
            deleted = cursor.rowcount
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return deleted

    def append_without_commit(
        self,
        connection: sqlite3.Connection,
        snapshots: Sequence[StateSnapshot],
    ) -> None:
        for snapshot in snapshots:
            payload = json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, allow_nan=False)
            history_id = hashlib.sha256(f"{self.household_id}\0{payload}".encode()).hexdigest()
            connection.execute(
                """INSERT OR IGNORE INTO state_history
                   (history_id, household_id, device_id, capability, payload,
                    observed_at, received_at, source_adapter_id, source_external_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    history_id,
                    self.household_id,
                    snapshot.device_id,
                    snapshot.capability,
                    payload,
                    snapshot.observed_at.isoformat(),
                    snapshot.received_at.isoformat(),
                    snapshot.source_ref.adapter_id,
                    snapshot.source_ref.external_id,
                ),
            )
        cutoff = self.clock.now() - timedelta(days=self.retention_days)
        connection.execute(
            """DELETE FROM state_history
               WHERE household_id = ? AND julianday(received_at) < julianday(?)""",
            (self.household_id, cutoff.astimezone(UTC).isoformat()),
        )

    async def list_history(
        self,
        *,
        device_ids: tuple[str, ...] | None = None,
        capabilities: tuple[str, ...] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[StateHistoryRecord]:
        if limit < 1:
            raise ValueError("history limit must be at least 1")
        if start is not None:
            _require_aware_datetime(start, "start")
        if end is not None:
            _require_aware_datetime(end, "end")
        if start is not None and end is not None and start >= end:
            raise ValueError("history start must be earlier than end")
        clauses = ["household_id = ?"]
        params: list[Any] = [self.household_id]
        if device_ids is not None:
            if not device_ids:
                clauses.append("1 = 0")
            else:
                clauses.append(_in_clause("device_id", len(device_ids)))
                params.extend(device_ids)
        if capabilities:
            clauses.append(_in_clause("capability", len(capabilities)))
            params.extend(capabilities)
        if start is not None:
            clauses.append("julianday(observed_at) >= julianday(?)")
            params.append(start.astimezone(UTC).isoformat())
        if end is not None:
            clauses.append("julianday(observed_at) < julianday(?)")
            params.append(end.astimezone(UTC).isoformat())
        params.append(min(limit, self._MAX_LIST_LIMIT))
        cursor = self.database.connection.execute(
            f"""SELECT history_id, payload FROM state_history
                WHERE {" AND ".join(clauses)}
                ORDER BY julianday(observed_at) DESC,
                         julianday(received_at) DESC, history_id DESC
                LIMIT ?""",
            params,
        )
        rows = cursor.fetchall()
        cursor.close()
        return [
            StateHistoryRecord(
                history_id=row[0],
                snapshot=StateSnapshot.model_validate(json.loads(row[1])),
            )
            for row in rows
        ]


class StateSnapshotRepository:
    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        history_repository: StateHistoryRepository | None = None,
    ) -> None:
        self.database = database
        self.history_repository = history_repository

    async def save(self, snapshot: StateSnapshot) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._save_without_commit(connection, snapshot)
            if self.history_repository is not None:
                self.history_repository.append_without_commit(connection, [snapshot])
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _save_without_commit(connection: sqlite3.Connection, snapshot: StateSnapshot) -> None:
        connection.execute(
            """INSERT INTO state_snapshots
               (device_id, capability, payload, observed_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(device_id, capability) DO UPDATE SET
               payload=excluded.payload, observed_at=excluded.observed_at""",
            (
                snapshot.device_id,
                snapshot.capability,
                json.dumps(snapshot.model_dump(mode="json"), sort_keys=True, allow_nan=False),
                snapshot.observed_at.isoformat(),
            ),
        )

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
        source_cursors: dict[tuple[str, str], SourceCursor] = {}
        raw_source_cursors = payload.get("source_cursors", {})
        if isinstance(raw_source_cursors, dict):
            for raw_key, raw_cursor in raw_source_cursors.items():
                source_id, separator, stream_id = str(raw_key).partition("::")
                if not separator or not source_id or not stream_id:
                    continue
                try:
                    source_cursor = SourceCursor.model_validate(raw_cursor)
                except (TypeError, ValidationError):
                    continue
                if (source_cursor.source_id, source_cursor.stream_id) == (source_id, stream_id):
                    source_cursors[(source_id, stream_id)] = source_cursor
        source_ordering_policies: dict[tuple[str, str], SourceOrderingPolicy] = {}
        raw_policies = payload.get("source_ordering_policies", {})
        if isinstance(raw_policies, dict):
            for raw_key, raw_policy in raw_policies.items():
                source_id, separator, stream_id = str(raw_key).partition("::")
                if not separator or not source_id or not stream_id:
                    continue
                try:
                    source_ordering_policies[(source_id, stream_id)] = SourceOrderingPolicy(
                        raw_policy
                    )
                except (TypeError, ValueError):
                    continue
        resync_required: dict[tuple[str, str], str] = {}
        raw_resync = payload.get("resync_required", {})
        if isinstance(raw_resync, dict):
            for raw_key, reason in raw_resync.items():
                source_id, separator, stream_id = str(raw_key).partition("::")
                if separator and source_id and stream_id and isinstance(reason, str) and reason:
                    resync_required[(source_id, stream_id)] = reason
        return StateStoreMetadata(
            inventory_revision=max(0, inventory_revision),
            version_counter=max(0, version_counter),
            state_versions=state_versions,
            inventory_fingerprint=inventory_fingerprint,
            source_cursors=source_cursors,
            source_ordering_policies=source_ordering_policies,
            resync_required=resync_required,
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
            "source_cursors": {
                f"{source_id}::{stream_id}": cursor.model_dump(mode="json")
                for (source_id, stream_id), cursor in metadata.source_cursors.items()
            },
            "source_ordering_policies": {
                f"{source_id}::{stream_id}": policy.value
                for (source_id, stream_id), policy in metadata.source_ordering_policies.items()
            },
            "resync_required": {
                f"{source_id}::{stream_id}": reason
                for (source_id, stream_id), reason in metadata.resync_required.items()
            },
        }
        self.database.connection.execute(
            """INSERT INTO runtime_state_metadata (id, payload, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               payload=excluded.payload, updated_at=excluded.updated_at""",
            (
                json.dumps(payload, sort_keys=True, allow_nan=False),
                self.clock.now().isoformat(),
            ),
        )
        self.database.connection.commit()


class RuntimeStatePersistenceRepository:
    """Atomically persist normalized snapshots and StateStore metadata."""

    def __init__(
        self,
        database: SQLiteDatabase,
        *,
        clock: Clock | None = None,
        history_repository: StateHistoryRepository | None = None,
    ) -> None:
        self.database = database
        self.clock = clock or SystemClock()
        self.history_repository = history_repository

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
                        json.dumps(
                            snapshot.model_dump(mode="json"),
                            sort_keys=True,
                            allow_nan=False,
                        ),
                        snapshot.observed_at.isoformat(),
                    ),
                )
            if self.history_repository is not None:
                self.history_repository.append_without_commit(connection, snapshots)
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

    async def delete_capability(
        self, device_id: str, capability: str, metadata: StateStoreMetadata
    ) -> None:
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM state_snapshots WHERE device_id = ? AND capability = ?",
                (device_id, capability),
            )
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
            "source_cursors": {
                f"{source_id}::{stream_id}": cursor.model_dump(mode="json")
                for (source_id, stream_id), cursor in metadata.source_cursors.items()
            },
            "source_ordering_policies": {
                f"{source_id}::{stream_id}": policy.value
                for (source_id, stream_id), policy in metadata.source_ordering_policies.items()
            },
            "resync_required": {
                f"{source_id}::{stream_id}": reason
                for (source_id, stream_id), reason in metadata.resync_required.items()
            },
        }
        connection.execute(
            """INSERT INTO runtime_state_metadata (id, payload, updated_at)
               VALUES (1, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               payload=excluded.payload, updated_at=excluded.updated_at""",
            (
                json.dumps(payload, sort_keys=True, allow_nan=False),
                self.clock.now().isoformat(),
            ),
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

    async def compatibility_diagnostics(
        self,
        *,
        tenant_id: str,
        household_id: str,
        deployment_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return compatibility_diagnostics(
            self.database.connection,
            record_kind="scheduled_plan",
            tenant_id=tenant_id,
            household_id=household_id,
            deployment_id=deployment_id,
            limit=limit,
            offset=offset,
        )

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
        return read_active_plan(row[0]), row[1]

    async def list_pending(self) -> list[Plan]:
        cursor = self.database.connection.execute(
            """SELECT payload FROM scheduled_plans
               WHERE status = 'pending' ORDER BY execute_at""",
        )
        rows = cursor.fetchall()
        cursor.close()
        compatible = []
        for row in rows:
            try:
                compatible.append(read_active_plan(row[0]))
            except DomainError:
                continue
        return compatible

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
        authority: dict[str, Any] | None = None,
    ) -> None:
        now = self.clock.now().isoformat()
        authority_payload = authority or {}
        serialized_authority = json.dumps(authority_payload, sort_keys=True)
        serialized_commands = json.dumps(
            [command.model_dump(mode="json") for command in commands], sort_keys=True
        )
        serialized_rule = json.dumps(rule.model_dump(mode="json"), sort_keys=True)
        existing = self.database.connection.execute(
            """SELECT status, authority_payload, template_payload, recurrence_payload
               FROM recurring_schedules
               WHERE schedule_id = ?""",
            (schedule_id,),
        ).fetchone()
        if existing is not None:
            stored_authority = json.loads(existing[1] or "{}")
            if (
                stored_authority != authority_payload
                or existing[2] != serialized_commands
                or existing[3] != serialized_rule
            ) and existing[0] != "cancelled":
                raise ValueError("recurring schedule intent conflicts with existing schedule")
            if existing[0] == "active":
                return
            self.database.connection.execute(
                """UPDATE recurring_schedules
                   SET template_payload = ?, recurrence_payload = ?,
                       next_execute_at = ?, status = 'active',
                       updated_at = ?, authority_payload = ?
                   WHERE schedule_id = ?""",
                (
                    serialized_commands,
                    serialized_rule,
                    next_execute_at.isoformat(),
                    now,
                    serialized_authority,
                    schedule_id,
                ),
            )
            self.database.connection.commit()
            return
        self.database.connection.execute(
            """INSERT INTO recurring_schedules
               (schedule_id, template_payload, recurrence_payload, next_execute_at,
                status, updated_at, authority_payload)
               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
            (
                schedule_id,
                serialized_commands,
                serialized_rule,
                next_execute_at.isoformat(),
                now,
                serialized_authority,
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

    async def get_authority(self, schedule_id: str) -> dict[str, Any] | None:
        cursor = self.database.connection.execute(
            "SELECT authority_payload FROM recurring_schedules WHERE schedule_id = ?",
            (schedule_id,),
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return None
        payload = json.loads(row[0] or "{}")
        if not isinstance(payload, dict):
            raise ValueError("recurring schedule authority payload must be an object")
        return payload

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


@dataclass(frozen=True)
class AutomationRuleRecord:
    rule: AutomationRule
    consent: AutomationConsent
    last_event_id: str | None = None
    last_fired_at: datetime | None = None


class AutomationRuleRepository:
    """Durable local-rule definitions and idempotent event claims."""

    def __init__(self, database: SQLiteDatabase, *, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    async def compatibility_diagnostics(
        self,
        *,
        tenant_id: str,
        household_id: str,
        deployment_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return compatibility_diagnostics(
            self.database.connection,
            record_kind="automation_rule",
            tenant_id=tenant_id,
            household_id=household_id,
            deployment_id=deployment_id,
            limit=limit,
            offset=offset,
        )

    async def save(self, rule: AutomationRule, consent: AutomationConsent) -> None:
        if consent.rule_digest != rule.definition_digest or consent.scope != rule.scope:
            raise ValueError("automation consent does not match rule definition or scope")
        if (
            consent.authority.tenant_id != rule.authority.tenant_id
            or consent.authority.household_id != rule.authority.household_id
        ):
            raise ValueError("automation consent does not match rule authority context")
        now = self.clock.now().isoformat()
        self.database.connection.execute(
            """INSERT INTO automation_rules
               (rule_id, payload, consent_payload, status, last_event_id,
                last_fired_at, updated_at)
               VALUES (?, ?, ?, ?, NULL, NULL, ?)
               ON CONFLICT(rule_id) DO UPDATE SET
               payload=excluded.payload,
               consent_payload=excluded.consent_payload,
               status=excluded.status,
               updated_at=excluded.updated_at""",
            (
                rule.id,
                json.dumps(rule.model_dump(mode="json"), sort_keys=True, allow_nan=False),
                json.dumps(consent.model_dump(mode="json"), sort_keys=True, allow_nan=False),
                rule.status.value,
                now,
            ),
        )
        self.database.connection.commit()

    async def update(
        self,
        rule: AutomationRule,
        consent: AutomationConsent,
        *,
        expected_definition_digest: str,
        expected_approval_id: str,
    ) -> bool:
        """Replace a rule version and invalidate event claims from its old version."""

        if consent.rule_digest != rule.definition_digest or consent.scope != rule.scope:
            raise ValueError("automation consent does not match rule definition or scope")
        if (
            consent.authority.tenant_id != rule.authority.tenant_id
            or consent.authority.household_id != rule.authority.household_id
        ):
            raise ValueError("automation consent does not match rule authority context")
        now = self.clock.now().isoformat()
        cursor = self.database.connection.execute(
            """UPDATE automation_rules
               SET payload = ?, consent_payload = ?, status = ?,
                   last_event_id = NULL, last_fired_at = NULL, updated_at = ?
               WHERE rule_id = ?
                 AND json_extract(payload, '$.definition_digest') = ?
                 AND json_extract(consent_payload, '$.approval_id') = ?""",
            (
                json.dumps(rule.model_dump(mode="json"), sort_keys=True, allow_nan=False),
                json.dumps(consent.model_dump(mode="json"), sort_keys=True, allow_nan=False),
                rule.status.value,
                now,
                rule.id,
                expected_definition_digest,
                expected_approval_id,
            ),
        )
        self.database.connection.commit()
        return cursor.rowcount > 0

    async def get(self, rule_id: str) -> AutomationRuleRecord | None:
        row = self.database.connection.execute(
            """SELECT payload, consent_payload, status, last_event_id, last_fired_at
               FROM automation_rules WHERE rule_id = ?""",
            (rule_id,),
        ).fetchone()
        if row is None:
            return None
        return AutomationRuleRecord(
            rule=read_active_rule(row[0]).model_copy(
                update={"status": AutomationRuleStatus(row[2])}
            ),
            consent=AutomationConsent.model_validate(json.loads(row[1])),
            last_event_id=row[3],
            last_fired_at=datetime.fromisoformat(row[4]) if row[4] else None,
        )

    async def list_all(self) -> list[AutomationRuleRecord]:
        rows = self.database.connection.execute(
            """SELECT payload, consent_payload, status, last_event_id, last_fired_at
               FROM automation_rules ORDER BY rule_id"""
        ).fetchall()
        compatible = []
        for row in rows:
            try:
                compatible.append(
                    AutomationRuleRecord(
                        rule=read_active_rule(row[0]).model_copy(
                            update={"status": AutomationRuleStatus(row[2])}
                        ),
                        consent=AutomationConsent.model_validate(json.loads(row[1])),
                        last_event_id=row[3],
                        last_fired_at=datetime.fromisoformat(row[4]) if row[4] else None,
                    )
                )
            except DomainError:
                continue
        return compatible

    async def set_status(self, rule_id: str, status: AutomationRuleStatus | str) -> bool:
        normalized = AutomationRuleStatus(status).value
        cursor = self.database.connection.execute(
            """UPDATE automation_rules SET status = ?, updated_at = ?
               WHERE rule_id = ?""",
            (normalized, self.clock.now().isoformat(), rule_id),
        )
        self.database.connection.commit()
        return cursor.rowcount > 0

    async def claim_event(
        self,
        rule_id: str,
        event_id: str,
        now: datetime,
        cooldown_seconds: int | None = None,
    ) -> bool:
        record = await self.get(rule_id)
        if record is None or record.rule.status is not AutomationRuleStatus.ENABLED:
            return False
        if record.last_event_id == event_id:
            return False
        cooldown = record.rule.cooldown_seconds if cooldown_seconds is None else cooldown_seconds
        if (
            record.last_fired_at is not None
            and (now - record.last_fired_at).total_seconds() < cooldown
        ):
            return False
        cursor = self.database.connection.execute(
            """UPDATE automation_rules
               SET last_event_id = ?, last_fired_at = ?, updated_at = ?
               WHERE rule_id = ? AND status = 'enabled'
                 AND last_event_id IS ? AND last_fired_at IS ?""",
            (
                event_id,
                now.isoformat(),
                self.clock.now().isoformat(),
                rule_id,
                record.last_event_id,
                record.last_fired_at.isoformat() if record.last_fired_at else None,
            ),
        )
        self.database.connection.commit()
        return cursor.rowcount == 1
