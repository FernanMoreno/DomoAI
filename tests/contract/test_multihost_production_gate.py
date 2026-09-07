from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.config.settings import Settings
from domoai.domain.coordination import LeaseScope
from domoai.domain.multihost_qualification import (
    REQUIRED_MULTIHOST_CHECKS,
    MultiHostQualificationCheck,
    MultiHostQualificationEvidence,
)


def _evidence() -> MultiHostQualificationEvidence:
    completed_at = datetime(2026, 9, 5, 12, tzinfo=UTC)
    return MultiHostQualificationEvidence(
        scope=LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        gateway_identity="gateway-serial-1",
        completed_at=completed_at,
        expires_at=completed_at + timedelta(days=1),
        checks=[
            MultiHostQualificationCheck(check_id=check_id, status="passed")
            for check_id in sorted(REQUIRED_MULTIHOST_CHECKS)
        ],
    )


def test_production_multihost_requires_evidence_path_and_gateway_identity() -> None:
    with pytest.raises(ValueError, match="qualification evidence"):
        Settings(multi_host_enabled=True, multi_host_production_enabled=True)

    with pytest.raises(ValueError, match="gateway identity"):
        Settings(
            multi_host_enabled=True,
            multi_host_production_enabled=True,
            multi_host_qualification_evidence_path=Path("qualification.json"),
        )


def test_production_multihost_rejects_evidence_without_multihost() -> None:
    with pytest.raises(ValueError, match="multi-host"):
        Settings(
            multi_host_production_enabled=True,
            multi_host_qualification_evidence_path=Path("qualification.json"),
            multi_host_gateway_identity="gateway-serial-1",
        )


def test_evidence_loader_rejects_symlink_and_invalid_json(tmp_path: Path) -> None:
    from domoai.domain.multihost_qualification import load_multihost_qualification_evidence

    invalid = tmp_path / "invalid.json"
    invalid.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="unavailable or not valid"):
        load_multihost_qualification_evidence(invalid)

    target = tmp_path / "target.json"
    target.write_text(_evidence().model_dump_json(), encoding="utf-8")
    link = tmp_path / "evidence-link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="unavailable or not valid"):
        load_multihost_qualification_evidence(link)
