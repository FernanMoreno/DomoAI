from __future__ import annotations

import pytest
from pydantic import ValidationError

from domoai.domain.digital_twin import (
    DigitalTwinCheck,
    DigitalTwinCheckStatus,
    DigitalTwinCoverage,
    DigitalTwinEvidence,
)


def _coverage() -> DigitalTwinCoverage:
    return DigitalTwinCoverage(
        adapters=["fixture", "knx"],
        domains=["light", "battery"],
        checks=["discover", "readback"],
    )


def test_digital_twin_evidence_serialization_is_canonical_and_scoped() -> None:
    evidence = DigitalTwinEvidence(
        run_id="twin-1",
        seed=187,
        plant_digest="a" * 64,
        trace_digest="b" * 64,
        coverage=_coverage(),
        checks=[DigitalTwinCheck(check_id="readback")],
    )

    assert evidence.scope == "digital_twin"
    assert evidence.status == "passed"
    assert evidence.canonical_json() == evidence.model_copy().canonical_json()
    assert '"scope":"digital_twin"' in evidence.canonical_json()


def test_digital_twin_evidence_rejects_physical_scope_and_secret_diagnostics() -> None:
    with pytest.raises(ValidationError):
        DigitalTwinEvidence(
            run_id="twin-physical",
            seed=1,
            plant_digest="a" * 64,
            trace_digest="b" * 64,
            coverage=_coverage(),
            scope="hardware",
        )

    with pytest.raises(ValidationError, match="sensitive"):
        DigitalTwinCheck(
            check_id="secret-check",
            details={"access_token": "must-not-enter-evidence"},
        )


def test_failed_check_makes_evidence_failed_and_keeps_safe_diagnostic() -> None:
    evidence = DigitalTwinEvidence(
        run_id="twin-failed",
        seed=187,
        plant_digest="a" * 64,
        trace_digest="b" * 64,
        coverage=_coverage(),
        checks=[
            DigitalTwinCheck(
                check_id="readback",
                status=DigitalTwinCheckStatus.FAILED,
                code="readback_mismatch",
                message="virtual readback did not converge",
            )
        ],
    )

    assert evidence.status == "failed"
    assert evidence.checks[0].code == "readback_mismatch"
