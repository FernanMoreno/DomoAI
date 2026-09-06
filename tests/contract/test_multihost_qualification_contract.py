import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from domoai.domain.coordination import LeaseScope


def _checks(status: str = "passed") -> list[object]:
    from domoai.domain.multihost_qualification import (
        REQUIRED_MULTIHOST_CHECKS,
        MultiHostQualificationCheck,
    )

    return [
        MultiHostQualificationCheck(check_id=check_id, status=status)
        for check_id in sorted(REQUIRED_MULTIHOST_CHECKS)
    ]


def _evidence(
    *,
    completed_at: datetime | None = None,
    qualification_environment: str | None = None,
):
    from domoai.domain.multihost_qualification import MultiHostQualificationEvidence

    completed_at = completed_at or datetime(2026, 9, 5, 12, tzinfo=UTC)
    return MultiHostQualificationEvidence(
        scope=LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        gateway_identity="gateway-serial-1",
        completed_at=completed_at,
        expires_at=completed_at + timedelta(days=7),
        checks=_checks(),
        **(
            {"qualification_environment": qualification_environment}
            if qualification_environment is not None
            else {}
        ),
    )


def _legacy_digest(evidence: object) -> str:
    payload = evidence.model_dump(  # type: ignore[union-attr]
        mode="json",
        exclude={"evidence_digest", "qualification_environment"},
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def test_passed_evidence_is_digest_bound_and_scope_specific() -> None:
    evidence = _evidence()

    assert evidence.status == "passed"
    assert evidence.evidence_digest.startswith("sha256:")
    assert evidence.qualifies(
        LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        gateway_identity="gateway-serial-1",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert not evidence.qualifies(
        LeaseScope(tenant_id="tenant", household_id="other", deployment_id="edge"),
        gateway_identity="gateway-serial-1",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )


def test_passed_lab_evidence_cannot_qualify_for_production() -> None:
    evidence = _evidence(qualification_environment="lab")

    assert evidence.status == "passed"
    assert not evidence.qualifies(
        LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        gateway_identity="gateway-serial-1",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )


def test_field_absent_legacy_evidence_loads_as_production_and_qualifies(tmp_path: Path) -> None:
    from domoai.domain.multihost_qualification import load_multihost_qualification_evidence

    evidence = _evidence()
    legacy_payload = evidence.model_dump(
        mode="json",
        exclude={"evidence_digest", "qualification_environment"},
    )
    legacy_payload["evidence_digest"] = _legacy_digest(evidence)
    evidence_path = tmp_path / "legacy-qualification.json"
    evidence_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    loaded = load_multihost_qualification_evidence(evidence_path)

    assert loaded.qualification_environment == "production"
    assert loaded.qualifies(
        LeaseScope(tenant_id="tenant", household_id="home", deployment_id="edge"),
        gateway_identity="gateway-serial-1",
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )


def test_explicit_lab_provenance_cannot_use_a_legacy_digest() -> None:
    from domoai.domain.multihost_qualification import MultiHostQualificationEvidence

    evidence = _evidence(qualification_environment="lab")

    with pytest.raises(ValueError, match="digest"):
        MultiHostQualificationEvidence(
            scope=evidence.scope,
            gateway_identity=evidence.gateway_identity,
            completed_at=evidence.completed_at,
            expires_at=evidence.expires_at,
            checks=evidence.checks,
            qualification_environment="lab",
            evidence_digest=_legacy_digest(evidence),
        )


def test_evidence_rejects_missing_or_tampered_checks_and_sensitive_details() -> None:
    from domoai.domain.multihost_qualification import (
        MultiHostQualificationCheck,
        MultiHostQualificationEvidence,
    )

    valid = _evidence()
    with pytest.raises(ValueError, match="required checks"):
        MultiHostQualificationEvidence(
            scope=valid.scope,
            gateway_identity=valid.gateway_identity,
            completed_at=valid.completed_at,
            expires_at=valid.expires_at,
            checks=valid.checks[:-1],
        )
    with pytest.raises(ValueError, match="sensitive"):
        MultiHostQualificationCheck(
            check_id="etcd_quorum", status="passed", details={"password": "never"}
        )
    with pytest.raises(ValueError, match="digest"):
        MultiHostQualificationEvidence(
            scope=valid.scope,
            gateway_identity=valid.gateway_identity,
            completed_at=valid.completed_at,
            expires_at=valid.expires_at,
            checks=valid.checks,
            evidence_digest="sha256:" + "0" * 64,
        )


def test_evidence_expiry_or_failure_cannot_qualify() -> None:
    from domoai.domain.multihost_qualification import MultiHostQualificationCheck

    evidence = _evidence()
    failed = type(evidence)(
        scope=evidence.scope,
        gateway_identity=evidence.gateway_identity,
        completed_at=evidence.completed_at,
        expires_at=evidence.expires_at,
        checks=[
            MultiHostQualificationCheck(
                check_id=check.check_id,
                status="failed" if check.check_id == "gateway_stale_epoch" else "passed",
            )
            for check in evidence.checks
        ],
    )
    assert failed.status == "failed"
    assert not failed.qualifies(
        evidence.scope,
        gateway_identity=evidence.gateway_identity,
        now=evidence.completed_at,
    )
    assert not evidence.qualifies(
        evidence.scope,
        gateway_identity=evidence.gateway_identity,
        now=evidence.expires_at,
    )
