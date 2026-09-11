"""Explicit offline adoption of reviewed legacy connection records.

Files are published before DB references. A failed DB transaction may leave an
owned orphan copy; the preserved source and idempotent manifest permit retry.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from domoai.domain.connections import ConnectionProfile
from domoai.domain.coordination import LeaseScope
from domoai.domain.models import StrictModel
from domoai.persistence.connection_repository import _SELECT_COLUMNS, _deserialize_profile
from domoai.persistence.postgres import PostgresDatabase
from domoai.persistence.sqlite import SQLiteDatabase
from domoai.security.secrets import FileSecretStore, SecretStoreError

_DIGEST = r"^[0-9a-f]{64}$"


class ConnectionAdoptionError(ValueError):
    """Sanitized conflict; no ownership change may be inferred from a failure."""


class AdoptionEntry(StrictModel):
    connection_id: UUID
    expected_profile_digest: str = Field(pattern=_DIGEST)
    source_credential_ref: str | None = None
    expected_ciphertext_digest: str | None = Field(default=None, pattern=_DIGEST)
    target_credential_ref: str | None = None

    @model_validator(mode="after")
    def validate_secret_copy(self) -> AdoptionEntry:
        values = (
            self.source_credential_ref,
            self.expected_ciphertext_digest,
            self.target_credential_ref,
        )
        if any(value is not None for value in values):
            if any(value is None for value in values):
                raise ValueError("secret adoption requires source, digest and destination")
            assert self.source_credential_ref is not None
            assert self.target_credential_ref is not None
            for reference in (self.source_credential_ref, self.target_credential_ref):
                if not reference.startswith("secret:"):
                    raise ValueError("secret adoption requires stored references")
                UUID(reference[7:])
            self.source_credential_ref = f"secret:{UUID(self.source_credential_ref[7:])}"
            self.target_credential_ref = f"secret:{UUID(self.target_credential_ref[7:])}"
            if self.source_credential_ref == self.target_credential_ref:
                raise ValueError("secret adoption must preserve its source")
        return self


class AdoptionManifest(StrictModel):
    schema_version: Literal["v1"] = "v1"
    scope: LeaseScope
    entries: tuple[AdoptionEntry, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_distinct_entries(self) -> AdoptionManifest:
        identifiers = [entry.connection_id for entry in self.entries]
        destinations = [
            entry.target_credential_ref
            for entry in self.entries
            if entry.target_credential_ref is not None
        ]
        sources = {entry.source_credential_ref for entry in self.entries}
        if len(set(identifiers)) != len(identifiers) or len(set(destinations)) != len(destinations):
            raise ValueError("adoption manifest contains duplicate destinations")
        if sources.intersection(destinations):
            raise ValueError("adoption destination cannot replace a source")
        return self


def profile_digest(profile: ConnectionProfile) -> str:
    payload = json.dumps(profile.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _entry_state(
    database: SQLiteDatabase | PostgresDatabase,
    manifest: AdoptionManifest,
    entry: AdoptionEntry,
    *,
    lock: bool,
) -> bool:
    connection = database.connection
    if lock:
        # An UPDATE locks the profile on PostgreSQL as well as SQLite. It does
        # not alter its content and avoids dialect-specific SELECT FOR UPDATE.
        connection.execute(
            "UPDATE connections SET id = id WHERE id = ?", (str(entry.connection_id),)
        )
    row = connection.execute(
        f"SELECT {_SELECT_COLUMNS} FROM connections WHERE id = ?", (str(entry.connection_id),)
    ).fetchone()
    if row is None:
        raise ConnectionAdoptionError("reviewed connection is unavailable")
    profile = _deserialize_profile(tuple(row))
    owner = connection.execute(
        """SELECT tenant_id, household_id, deployment_id FROM connection_ownership
           WHERE connection_id = ?""",
        (str(entry.connection_id),),
    ).fetchone()
    expected_scope = (
        manifest.scope.tenant_id,
        manifest.scope.household_id,
        manifest.scope.deployment_id,
    )
    bound = owner is not None
    if bound and tuple(owner) != expected_scope:
        raise ConnectionAdoptionError("connection already belongs to another scope")
    if entry.source_credential_ref is not None:
        expected_ref = entry.target_credential_ref if bound else entry.source_credential_ref
        if profile.credential_ref != expected_ref:
            raise ConnectionAdoptionError("connection credential changed after review")
        profile = profile.model_copy(update={"credential_ref": entry.source_credential_ref})
    elif profile.credential_ref and profile.credential_ref.startswith("secret:"):
        raise ConnectionAdoptionError("stored credential requires explicit adoption")
    if profile_digest(profile) != entry.expected_profile_digest:
        raise ConnectionAdoptionError("connection changed after review")
    return bound


def adopt_connections(
    database: SQLiteDatabase | PostgresDatabase,
    manifest: AdoptionManifest,
    *,
    store: FileSecretStore | None = None,
    apply: bool = False,
    rollback: bool = False,
) -> dict[str, object]:
    """Keep every credential stable from review through commit, including rollback."""

    if store is not None and store.scope != manifest.scope:
        raise ConnectionAdoptionError("secret store does not match reviewed scope")
    with ExitStack() as locks:
        if apply and store is not None:
            references = [
                reference
                for entry in manifest.entries
                for reference in (entry.source_credential_ref, entry.target_credential_ref)
                if reference is not None
            ]
            locks.enter_context(store.lock_references(references))
        return _adopt_connections_locked(
            database, manifest, store=store, apply=apply, rollback=rollback
        )


def _adopt_connections_locked(
    database: SQLiteDatabase | PostgresDatabase,
    manifest: AdoptionManifest,
    *,
    store: FileSecretStore | None = None,
    apply: bool = False,
    rollback: bool = False,
) -> dict[str, object]:
    """Inspect by default; apply/rollback only in an explicitly offline window."""

    if store is not None and store.scope != manifest.scope:
        raise ConnectionAdoptionError("secret store does not match reviewed scope")
    states = [_entry_state(database, manifest, entry, lock=False) for entry in manifest.entries]
    try:
        for entry in manifest.entries:
            if entry.source_credential_ref is None:
                continue
            if store is None:
                raise ConnectionAdoptionError("secret adoption requires the reviewed master key")
            assert entry.expected_ciphertext_digest is not None
            assert entry.target_credential_ref is not None
            store._copy_legacy_locked(
                entry.source_credential_ref,
                expected_digest=entry.expected_ciphertext_digest,
                target_reference=entry.target_credential_ref,
                dry_run=True,
            )
        if not apply:
            return {
                "action": "inspect_rollback" if rollback else "inspect",
                "changed": 0,
                "pending": sum(bound if rollback else not bound for bound in states),
            }
        if not rollback:
            for entry in manifest.entries:
                if entry.source_credential_ref is not None:
                    assert store is not None and entry.expected_ciphertext_digest is not None
                    assert entry.target_credential_ref is not None
                    store._copy_legacy_locked(
                        entry.source_credential_ref,
                        expected_digest=entry.expected_ciphertext_digest,
                        target_reference=entry.target_credential_ref,
                        dry_run=False,
                    )
    except SecretStoreError:
        raise ConnectionAdoptionError("reviewed credential cannot be adopted") from None

    connection = database.connection
    changed = 0
    try:
        connection.execute("BEGIN IMMEDIATE")
        # Stable lock ordering prevents multi-entry manifests deadlocking.
        for entry in sorted(manifest.entries, key=lambda value: str(value.connection_id)):
            bound = _entry_state(database, manifest, entry, lock=True)
            if bound != rollback:
                continue
            if rollback:
                connection.execute(
                    "DELETE FROM connection_ownership WHERE connection_id = ?",
                    (str(entry.connection_id),),
                )
                if entry.source_credential_ref is not None:
                    connection.execute(
                        "UPDATE connections SET credential_ref = ? WHERE id = ?",
                        (entry.source_credential_ref, str(entry.connection_id)),
                    )
            else:
                if entry.target_credential_ref is not None:
                    connection.execute(
                        "UPDATE connections SET credential_ref = ? WHERE id = ?",
                        (entry.target_credential_ref, str(entry.connection_id)),
                    )
                connection.execute(
                    """INSERT INTO connection_ownership
                       (connection_id, tenant_id, household_id, deployment_id, bound_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(entry.connection_id),
                        manifest.scope.tenant_id,
                        manifest.scope.household_id,
                        manifest.scope.deployment_id,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            changed += 1
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    return {"action": "rollback" if rollback else "apply", "changed": changed}
