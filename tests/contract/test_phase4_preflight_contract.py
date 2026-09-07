from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.domain.phase4_preflight import (
    PHYSICAL_GATE_IDS,
    SOFTWARE_GATE_IDS,
    Phase4Gate,
    Phase4GateKind,
    Phase4GateStatus,
    Phase4PreflightReport,
    Phase4PreflightStatus,
)


def _gate(
    gate_id: str,
    *,
    kind: Phase4GateKind,
    status: Phase4GateStatus = Phase4GateStatus.PASSED,
    evidence_scope: str | None = None,
    details: dict[str, object] | None = None,
) -> Phase4Gate:
    return Phase4Gate(
        gate_id=gate_id,
        kind=kind,
        status=status,
        evidence_scope=evidence_scope
        or (
            "external_blocked"
            if status is Phase4GateStatus.BLOCKED
            else ("external_verified" if kind is Phase4GateKind.PHYSICAL else kind.value)
        ),
        details=details or {},
    )


def _report(
    *, physical_status: Phase4GateStatus = Phase4GateStatus.BLOCKED
) -> Phase4PreflightReport:
    return Phase4PreflightReport(
        run_id="preflight-1",
        seed=187,
        started_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        completed_at=datetime(2026, 9, 6, 12, 1, tzinfo=UTC),
        software_gates=[
            _gate("digital_twin", kind=Phase4GateKind.SOFTWARE),
            _gate("process_lab", kind=Phase4GateKind.PROCESS_LAB),
        ],
        physical_gates=[
            _gate(
                gate_id,
                kind=Phase4GateKind.PHYSICAL,
                status=physical_status,
                details={"reason": "hardware_not_available"},
            )
            for gate_id in sorted(PHYSICAL_GATE_IDS)
        ],
        residuals=["battery_hil", "live_protocol_commissioning", "external_provider"],
    )


def test_preflight_derives_blocked_status_without_claiming_hardware() -> None:
    report = _report()

    assert report.status is Phase4PreflightStatus.BLOCKED_EXTERNAL_DEPENDENCY
    assert report.qualification_environment == "local_preflight"
    assert all(gate.evidence_scope == "external_blocked" for gate in report.physical_gates)
    assert '"evidence_scope":"hardware"' not in report.canonical_json()


def test_preflight_passes_only_when_all_gates_pass() -> None:
    report = _report(physical_status=Phase4GateStatus.PASSED)

    assert report.status is Phase4PreflightStatus.PASSED


def test_preflight_rejects_secret_details_and_hardware_scope() -> None:
    with pytest.raises(ValidationError, match="secret-safe"):
        Phase4Gate(
            gate_id="digital_twin",
            kind=Phase4GateKind.SOFTWARE,
            status=Phase4GateStatus.PASSED,
            evidence_scope="software",
            details={"api_token": "must-not-appear"},
        )

    with pytest.raises(ValidationError):
        Phase4Gate(
            gate_id="battery_hil",
            kind=Phase4GateKind.PHYSICAL,
            status=Phase4GateStatus.PASSED,
            evidence_scope="hardware",
        )


def test_preflight_requires_exact_gate_sets_and_ordered_timestamps() -> None:
    report = _report()
    with pytest.raises(ValidationError):
        invalid_payload = report.model_dump()
        invalid_payload["software_gates"] = invalid_payload["software_gates"][:1]
        invalid_payload["completed_at"] = report.started_at - timedelta(seconds=1)
        Phase4PreflightReport.model_validate(invalid_payload)

    with pytest.raises(ValidationError):
        Phase4PreflightReport(
            run_id="preflight-1",
            seed=187,
            started_at=report.started_at,
            completed_at=report.completed_at,
            software_gates=report.software_gates,
            physical_gates=report.physical_gates[:-1],
            residuals=report.residuals,
        )


def test_gate_id_constants_describe_the_contract() -> None:
    assert SOFTWARE_GATE_IDS == frozenset({"digital_twin", "process_lab"})
    assert PHYSICAL_GATE_IDS == frozenset(
        {"battery_hil", "live_protocol_commissioning", "external_provider"}
    )
