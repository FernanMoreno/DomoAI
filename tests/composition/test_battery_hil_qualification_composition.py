from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.battery_qualification import (
    REQUIRED_HIL_CHECKS,
    BatteryHILEvidence,
    BatteryQualificationError,
    battery_binding_digest,
)
from domoai.config.settings import Settings
from tests.composition.test_battery_dispatch_profile_composition import _binding


def _settings(tmp_path, *, profile_path, evidence_path=None, production=False) -> Settings:
    return Settings(
        database_path=tmp_path / "qualification.sqlite3",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6.0,
        solar_tilt=30.0,
        solar_azimuth=0.0,
        solar_performance_ratio=0.82,
        battery_dispatch_profile_path=profile_path,
        battery_hil_evidence_path=evidence_path,
        battery_dispatch_production=production,
    )


def _passing_evidence(binding, **overrides) -> BatteryHILEvidence:
    payload = {
        "status": "passed",
        "profile_digest": battery_binding_digest(binding),
        "hardware_id": "lab-inverter-1",
        "firmware_version": "1.2.3",
        "completed_at": datetime.now(UTC),
        "checks": {check: True for check in REQUIRED_HIL_CHECKS},
        "run_id": "hil-run-test",
        "provider_id": binding.provider_id,
        "runtime_binding_digest": battery_binding_digest(binding),
        "takeover_evidence_digest": "sha256:" + "a" * 64,
        "hardware_identity_observed": True,
        "firmware_identity_observed": True,
        "qualification_expires_at": datetime.now(UTC) + timedelta(hours=24),
        **overrides,
    }
    return BatteryHILEvidence.model_validate(payload)


def test_operator_identity_labels_do_not_count_as_observed_hardware_evidence() -> None:
    binding = _binding()
    evidence = _passing_evidence(binding)

    assert evidence.qualifies(binding) is False


def test_manual_check_status_preserves_not_exercised_as_distinct_state() -> None:
    binding = _binding()
    evidence = _passing_evidence(
        binding,
        manual_attestations={"restart_no_replay": "process restart was not exercised"},
        manual_check_status={"restart_no_replay": "not_exercised"},
    )

    assert evidence.manual_check_status["restart_no_replay"] == "not_exercised"
    assert evidence.qualifies(binding) is False


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_labels_matching_hil_evidence_and_exposes_status(tmp_path) -> None:
    binding = _binding()
    profile_path = tmp_path / "battery-profile.json"
    profile_path.write_text(json.dumps(binding.model_dump(mode="json")), encoding="utf-8")
    evidence_path = tmp_path / "battery-hil.json"
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "status": "passed",
                "profile_digest": battery_binding_digest(binding),
                "hardware_id": "lab-inverter-1",
                "firmware_version": "1.2.3",
                "completed_at": datetime.now(UTC).isoformat(),
                "checks": {check: True for check in REQUIRED_HIL_CHECKS},
                "run_id": "hil-run-1",
                "test_software_version": "test-sha",
                "provider_id": binding.provider_id,
                "runtime_binding_digest": battery_binding_digest(binding),
                "takeover_evidence_digest": "sha256:" + "a" * 64,
                "hardware_identity_observed": True,
                "firmware_identity_observed": True,
                "identity_observation": {
                    "hardware_id": "lab-inverter-1",
                    "firmware_version": "1.2.3",
                    "source": "trusted_attestation",
                    "observed_at": datetime.now(UTC).isoformat(),
                },
                "manual_attestations": {
                    "native_scheduler_conflict": "verified by lab operator",
                    "restart_no_replay": "verified by process restart test",
                },
                "manual_check_status": {
                    "native_scheduler_conflict": "verified",
                    "restart_no_replay": "verified",
                },
                "qualification_expires_at": (
                    (datetime.now(UTC) + timedelta(hours=24)).isoformat()
                ),
            }
        ),
        encoding="utf-8",
    )

    runtime = await build_runtime(
        _settings(
            tmp_path,
            profile_path=profile_path,
            evidence_path=evidence_path,
            production=True,
        ),
        adapter=SimulatedHomeAdapter(),
    )
    try:
        assert runtime.battery_qualification == "hil-qualified"
    finally:
        await runtime.close()


@pytest.mark.composition
@pytest.mark.asyncio
async def test_production_battery_dispatch_fails_closed_without_hil(tmp_path) -> None:
    binding = _binding()
    profile_path = tmp_path / "battery-profile.json"
    profile_path.write_text(json.dumps(binding.model_dump(mode="json")), encoding="utf-8")

    with pytest.raises(BatteryQualificationError, match="requires passing matching HIL"):
        await build_runtime(
            _settings(tmp_path, profile_path=profile_path, production=True),
            adapter=SimulatedHomeAdapter(),
        )
