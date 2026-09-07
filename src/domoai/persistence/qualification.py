"""Persistence for non-authoritative commissioning qualifications."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from domoai.domain.commissioning import CommissioningQualification
from domoai.persistence.sqlite import SQLiteDatabase


class SQLiteCommissioningQualificationRepository:
    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save(self, qualification_id: str, qualification: CommissioningQualification) -> None:
        payload = json.dumps(qualification.model_dump(mode="json"), sort_keys=True)
        self.database.connection.execute(
            """INSERT INTO commissioning_qualifications
               (id, status, payload, authority_payload, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
               status=excluded.status,
               payload=excluded.payload,
               authority_payload=excluded.authority_payload,
               updated_at=excluded.updated_at""",
            (
                qualification_id,
                qualification.status.value,
                payload,
                json.dumps(qualification.authority.model_dump(mode="json"), sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.database.connection.commit()

    async def get(self, qualification_id: str) -> CommissioningQualification | None:
        row = self.database.connection.execute(
            "SELECT payload FROM commissioning_qualifications WHERE id = ?",
            (qualification_id,),
        ).fetchone()
        return CommissioningQualification.model_validate(json.loads(row[0])) if row else None


__all__ = ["SQLiteCommissioningQualificationRepository"]
