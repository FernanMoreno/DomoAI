"""Durable, content-addressed documents used by P1 onboarding flows."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from domoai.domain.p1_completion import validate_secret_safe_document
from domoai.persistence.sqlite import SQLiteDatabase


@dataclass(frozen=True)
class StoredConnectionDocument:
    document_ref: str
    kind: str
    digest: str
    payload: dict[str, Any]
    provenance: dict[str, Any]


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class ConnectionDocumentRepository:
    """Insert-only content-addressed storage for validated connection artifacts."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        provenance: dict[str, Any],
    ) -> StoredConnectionDocument:
        if not kind or len(kind) > 64 or not kind.replace("_", "").isalnum():
            raise ValueError("document kind must be a simple identifier")
        validate_secret_safe_document(payload, field_name="payload")
        validate_secret_safe_document(provenance, field_name="provenance")
        payload_json = canonical_json(payload)
        provenance_json = canonical_json(provenance)
        digest = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        document_ref = f"{kind}:{digest[:32]}"
        connection = self.database.connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO connection_documents
                   (document_ref, kind, digest, payload_json, provenance_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (document_ref, kind, digest, payload_json, provenance_json),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return StoredConnectionDocument(document_ref, kind, digest, payload, provenance)

    async def get(self, document_ref: str) -> StoredConnectionDocument | None:
        cursor = self.database.connection.execute(
            """SELECT document_ref, kind, digest, payload_json, provenance_json
               FROM connection_documents WHERE document_ref = ?""",
            (document_ref,),
        )
        row = cursor.fetchone()
        cursor.close()
        if row is None:
            return None
        return StoredConnectionDocument(
            document_ref=row[0],
            kind=row[1],
            digest=row[2],
            payload=json.loads(row[3]),
            provenance=json.loads(row[4]),
        )


__all__ = ["ConnectionDocumentRepository", "StoredConnectionDocument", "canonical_json"]
