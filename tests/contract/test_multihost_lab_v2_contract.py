from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.lab.multihost_lab_v2 import LabRecoveryReport, LabScenarioResult


def _scenario(**overrides: object) -> LabScenarioResult:
    payload: dict[str, object] = {
        "run_id": "run-1",
        "scenario_id": "ownership-race",
        "status": "passed",
        "instance_ids": ["host-a", "host-b"],
        "observations": {"owner_count": 1, "unauthorized_writes": 0},
        "diagnostics": [],
        "duration_ms": 12,
    }
    payload.update(overrides)
    return LabScenarioResult.model_validate(payload)


def test_lab_report_is_explicitly_non_production() -> None:
    scenario = _scenario()
    report = LabRecoveryReport(
        run_id="run-1",
        started_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
        completed_at=datetime(2026, 9, 6, 12, 1, tzinfo=UTC),
        scenarios=[scenario],
        cleanup={"containers": "removed"},
    )

    assert report.qualification_environment == "lab"
    assert report.model_dump(mode="json")["qualification_environment"] == "lab"


def test_lab_report_rejects_secret_shaped_observations() -> None:
    with pytest.raises(ValidationError, match="secret-safe"):
        _scenario(observations={"client_password": "not-allowed"})


def test_lab_report_requires_ordered_timestamps_and_lab_provenance() -> None:
    with pytest.raises(ValidationError):
        LabRecoveryReport(
            run_id="run-1",
            qualification_environment="production",
            started_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
            completed_at=datetime(2026, 9, 6, 11, 59, tzinfo=UTC),
            scenarios=[_scenario()],
            cleanup={},
        )

    with pytest.raises(ValidationError):
        LabRecoveryReport(
            run_id="run-1",
            started_at=datetime(2026, 9, 6, 12, tzinfo=UTC),
            completed_at=datetime(2026, 9, 6, 12, tzinfo=UTC) - timedelta(seconds=1),
            scenarios=[_scenario()],
            cleanup={},
        )
