from datetime import UTC, datetime

import pytest

from domoai.domain.commissioning import CommissioningQualification, CommissioningQualificationStatus
from domoai.domain.models import AuthorityContext
from domoai.persistence.qualification import SQLiteCommissioningQualificationRepository
from domoai.persistence.sqlite import SQLiteDatabase


@pytest.mark.asyncio
async def test_commissioning_qualification_round_trips_without_secret_material(tmp_path) -> None:
    database = SQLiteDatabase(tmp_path / "commissioning.sqlite3")
    await database.initialize()
    try:
        qualification = CommissioningQualification(
            authority=AuthorityContext(
                tenant_id="tenant-a",
                household_id="home-a",
                household_ids=["home-a"],
                principal_id="owner-a",
                roles=["owner"],
            ),
            candidate_digest="a" * 64,
            evidence_digest="sha256:" + "b" * 64,
            status=CommissioningQualificationStatus.BLOCKED_EXTERNAL_DEPENDENCY,
            verified_checks=["identity"],
            blockers=["physical_evidence_required"],
            checked_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
        repository = SQLiteCommissioningQualificationRepository(database)

        await repository.save("qualification-1", qualification)
        restored = await repository.get("qualification-1")

        assert restored == qualification
        raw = database.connection.execute(
            "SELECT payload FROM commissioning_qualifications WHERE id = ?",
            ("qualification-1",),
        ).fetchone()[0]
        assert "token" not in raw.casefold()
        assert "secret" not in raw.casefold()
    finally:
        await database.close()
