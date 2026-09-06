from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.application.coordination import DeterministicLeaseCoordinator
from domoai.application.runtime_factory import build_runtime
from domoai.config.settings import Settings
from domoai.domain.coordination import LeaseScope
from domoai.domain.multihost_qualification import (
    REQUIRED_MULTIHOST_CHECKS,
    MultiHostQualificationCheck,
    MultiHostQualificationEvidence,
)
from domoai.runtime.clock import FixedClock


def _write_evidence(
    path: Path,
    *,
    scope: LeaseScope,
    qualification_environment: str = "production",
) -> None:
    completed_at = datetime(2026, 9, 5, 12, tzinfo=UTC)
    evidence = MultiHostQualificationEvidence(
        scope=scope,
        gateway_identity="gateway-serial-1",
        completed_at=completed_at,
        expires_at=completed_at + timedelta(days=1),
        qualification_environment=qualification_environment,
        checks=[
            MultiHostQualificationCheck(check_id=check_id, status="passed")
            for check_id in sorted(REQUIRED_MULTIHOST_CHECKS)
        ],
    )
    path.write_text(evidence.model_dump_json(), encoding="utf-8")


@pytest.mark.asyncio
async def test_production_multihost_fails_closed_before_external_coordinator_build(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "qualification.json"
    _write_evidence(
        evidence_path,
        scope=LeaseScope(tenant_id="wrong-tenant", household_id="home", deployment_id="edge"),
    )
    settings = Settings(
        multi_host_enabled=True,
        multi_host_production_enabled=True,
        multi_host_qualification_evidence_path=evidence_path,
        multi_host_gateway_identity="gateway-serial-1",
        mcp_tenant_id="tenant",
        mcp_household_id="home",
        mcp_deployment_id="edge",
        database_path=tmp_path / "runtime.sqlite3",
    )

    with pytest.raises(ValueError, match="matching passed qualification evidence"):
        await build_runtime(
            settings,
            clock=FixedClock(datetime(2026, 9, 5, 13, tzinfo=UTC)),
        )

    assert not settings.database_path.exists()


@pytest.mark.asyncio
async def test_production_multihost_rejects_lab_evidence_before_storage_with_injected_coordinator(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "qualification.json"
    _write_evidence(
        evidence_path,
        scope=LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        qualification_environment="lab",
    )
    now = datetime(2026, 9, 5, 13, tzinfo=UTC)
    clock = FixedClock(now)
    settings = Settings(
        multi_host_enabled=True,
        multi_host_production_enabled=True,
        multi_host_qualification_evidence_path=evidence_path,
        multi_host_gateway_identity="gateway-serial-1",
        mcp_tenant_id="tenant",
        mcp_household_id="home",
        mcp_deployment_id="edge",
        database_path=tmp_path / "runtime.sqlite3",
    )

    with pytest.raises(ValueError, match="matching passed qualification evidence"):
        await build_runtime(
            settings,
            clock=clock,
            lease_coordinator=DeterministicLeaseCoordinator(clock=clock),
        )

    assert not settings.database_path.exists()
