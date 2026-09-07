from datetime import UTC, datetime, timedelta

import pytest

from domoai.application.commissioning import CommissioningService
from domoai.domain.commissioning import (
    CommissioningCheck,
    CommissioningEvidence,
    CommissioningEvidenceClass,
    CommissioningQualificationStatus,
)
from domoai.domain.models import AdapterSnapshot, AuthorityContext
from domoai.runtime.clock import FixedClock
from domoai.runtime.registry import DeviceRegistry

NOW = datetime(2026, 9, 4, tzinfo=UTC)


def _registry() -> DeviceRegistry:
    registry = DeviceRegistry()
    registry.apply_snapshot(
        AdapterSnapshot(
            source_entities=[
                {
                    "entity_id": "fixture.battery",
                    "source_device_id": "battery-1",
                    "canonical_id": "garage.battery",
                    "identity_keys": ["fixture:battery-1"],
                    "connections": ["fixture:bus:battery-1"],
                    "name": "Garage Battery",
                    "domain": "energy",
                    "semantic_type": "energy",
                    "capabilities": [
                        {
                            "name": "battery.soc",
                            "kind": "number",
                            "unit": "%",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery.power",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery.capacity",
                            "kind": "number",
                            "unit": "kWh",
                            "readable": True,
                            "writable": False,
                        },
                        {
                            "name": "battery_control",
                            "kind": "number",
                            "unit": "kW",
                            "readable": True,
                            "writable": True,
                            "commands": ["charge", "discharge", "stop"],
                        },
                    ],
                    "available": True,
                }
            ]
        ),
        "fixture",
    )
    return registry


def _report() -> tuple[CommissioningService, object]:
    service = CommissioningService(_registry(), clock=FixedClock(NOW))
    report = service.inspect(
        runtime_revision="runtime-1",
        authority=AuthorityContext(
            tenant_id="tenant-a",
            household_id="home-a",
            household_ids=["home-a"],
            principal_id="owner-a",
            roles=["owner"],
        ),
        persist=False,
    )
    return service, report


def _evidence(
    report: object, *, evidence_class: CommissioningEvidenceClass
) -> CommissioningEvidence:
    candidate = report.candidates[0]
    return CommissioningEvidence(
        authority=report.authority,
        evidence_id="evidence-1",
        candidate_digest=candidate.candidate_digest,
        observed_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        evidence_class=evidence_class,
        checks=[
            CommissioningCheck(check_id="identity", status="passed", detail="identity stable"),
            CommissioningCheck(check_id="read_observation", status="passed", detail="read ok"),
            CommissioningCheck(check_id="safe_actuation", status="passed", detail="bounded"),
            CommissioningCheck(check_id="readback", status="passed", detail="readback ok"),
        ],
    )


def test_hardware_evidence_qualifies_matching_candidate_without_authority_creation() -> None:
    service, report = _report()

    result = service.verify_evidence(
        report,
        _evidence(report, evidence_class=CommissioningEvidenceClass.HARDWARE),
    )

    assert result.status is CommissioningQualificationStatus.QUALIFIED
    assert result.authority_created is False
    assert result.evidence_digest.startswith("sha256:")


def test_simulation_evidence_is_blocked_as_external_dependency() -> None:
    service, report = _report()

    result = service.verify_evidence(
        report,
        _evidence(report, evidence_class=CommissioningEvidenceClass.SIMULATION),
    )

    assert result.status is CommissioningQualificationStatus.BLOCKED_EXTERNAL_DEPENDENCY
    assert "physical_evidence_required" in result.blockers


@pytest.mark.parametrize("change", ["candidate_digest", "expires_at"])
def test_mismatched_or_expired_evidence_is_rejected(change: str) -> None:
    service, report = _report()
    evidence = _evidence(report, evidence_class=CommissioningEvidenceClass.HARDWARE)
    if change == "candidate_digest":
        evidence = evidence.model_copy(update={"candidate_digest": "0" * 64})
    else:
        evidence = evidence.model_copy(update={"expires_at": NOW})

    result = service.verify_evidence(report, evidence)

    assert result.status is CommissioningQualificationStatus.REJECTED
    assert result.blockers
