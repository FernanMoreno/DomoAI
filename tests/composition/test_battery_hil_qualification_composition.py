from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.battery_qualification import (
    MANUAL_HIL_CHECKS,
    REQUIRED_HIL_CHECKS,
    BatteryHILEvidence,
    BatteryQualificationError,
    battery_binding_digest,
    battery_identity_digest,
)
from domoai.config.settings import Settings
from domoai.lab.battery_simulator import BatterySimulationProfile, BatterySimulator
from tests.composition.test_battery_dispatch_profile_composition import (
    _binding,
    _MatchingControlAdapter,
)


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


@pytest.mark.composition
@pytest.mark.asyncio
async def test_runtime_labels_matching_hil_evidence_and_exposes_status(tmp_path) -> None:
    binding = _binding()
    profile_path = tmp_path / "battery-profile.json"
    profile_path.write_text(json.dumps(binding.model_dump(mode="json")), encoding="utf-8")
    evidence_path = tmp_path / "battery-hil.json"
    completed_at = datetime.now(UTC)
    profile_digest = battery_binding_digest(binding)
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "status": "passed",
                "profile_digest": profile_digest,
                "hardware_id": "lab-inverter-1",
                "firmware_version": "1.2.3",
                "completed_at": completed_at.isoformat(),
                "checks": {check: True for check in REQUIRED_HIL_CHECKS},
                "run_id": "hil-run-1",
                "provider_id": binding.provider_id,
                "runtime_binding_digest": profile_digest,
                "takeover_evidence_digest": "sha256:" + "a" * 64,
                "hardware_identity_observed": True,
                "firmware_identity_observed": True,
                "identity_observed_at": completed_at.isoformat(),
                "identity_evidence_digest": battery_identity_digest(
                    hardware_id="lab-inverter-1",
                    firmware_version="1.2.3",
                    provider_id=binding.provider_id,
                    profile_digest=profile_digest,
                    observed_at=completed_at,
                ),
                "test_software_version": "test-sha",
                "manual_attestations": {
                    "native_scheduler_conflict": "verified on the qualification bench",
                    "restart_no_replay": "verified after controlled process restart",
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
        adapter=_MatchingControlAdapter(),
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


@pytest.mark.composition
def test_qualification_rejects_passed_artifact_without_manual_check_evidence() -> None:
    binding = _binding()
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    evidence = BatteryHILEvidence(
        status="passed",
        profile_digest=battery_binding_digest(binding),
        hardware_id="observed-inverter",
        firmware_version="observed-firmware",
        completed_at=now,
        checks={check: True for check in REQUIRED_HIL_CHECKS},
        run_id="hil-missing-manual-evidence",
        provider_id=binding.provider_id,
        runtime_binding_digest=battery_binding_digest(binding),
        takeover_evidence_digest="sha256:" + "a" * 64,
        hardware_identity_observed=True,
        firmware_identity_observed=True,
        qualification_expires_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
    )

    assert evidence.qualifies(binding, now=now) is False


@pytest.mark.composition
def test_qualification_rejects_expired_or_unobserved_identity_evidence() -> None:
    binding = _binding()
    completed_at = datetime(2026, 8, 26, 12, tzinfo=UTC)
    evidence = BatteryHILEvidence(
        status="passed",
        profile_digest=battery_binding_digest(binding),
        hardware_id="observed-inverter",
        firmware_version="observed-firmware",
        completed_at=completed_at,
        checks={check: True for check in REQUIRED_HIL_CHECKS},
        run_id="hil-expiry-and-identity",
        provider_id=binding.provider_id,
        runtime_binding_digest=battery_binding_digest(binding),
        takeover_evidence_digest="sha256:" + "a" * 64,
        hardware_identity_observed=True,
        firmware_identity_observed=True,
        identity_observed_at=completed_at,
        identity_evidence_digest=battery_identity_digest(
            hardware_id="observed-inverter",
            firmware_version="observed-firmware",
            provider_id=binding.provider_id,
            profile_digest=battery_binding_digest(binding),
            observed_at=completed_at,
        ),
        test_software_version="test-sha",
        manual_check_status={check: "verified" for check in MANUAL_HIL_CHECKS},
        qualification_expires_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
    )

    assert evidence.qualifies(binding, now=datetime(2026, 8, 26, 13, tzinfo=UTC)) is True
    assert evidence.qualifies(binding, now=datetime(2026, 8, 27, 12, tzinfo=UTC)) is False
    assert evidence.model_copy(update={"hardware_identity_observed": False}).qualifies(
        binding, now=datetime(2026, 8, 26, 13, tzinfo=UTC)
    ) is False


def _lab_simulation_profile() -> BatterySimulationProfile:
    return BatterySimulationProfile(
        provider_id="lab-battery-simulator",
        device_id="lab-battery-1",
        capacity_kwh=10.0,
        initial_soc_kwh=5.0,
        min_soc_kwh=2.0,
        max_soc_kwh=9.0,
        max_charge_kw=4.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        tick_seconds=1.0,
    )


@pytest.mark.composition
def test_lab_simulation_profile_cannot_be_hashed_as_a_qualification_binding() -> None:
    """The simulator's own profile type cannot even enter the digest pipeline.

    ``battery_binding_digest`` requires a real ``DispatchableBatteryBinding``
    (a Pydantic model); the lab simulator's profile is a plain dataclass with
    no production semantic fields (capacity evidence, actuator wiring, trust
    policy). There is no accidental path from a lab profile to a valid
    ``profile_digest``.
    """

    lab_profile = _lab_simulation_profile()

    with pytest.raises(AttributeError):
        battery_binding_digest(lab_profile)  # type: ignore[arg-type]


@pytest.mark.composition
def test_lab_simulator_evidence_cannot_qualify_a_real_production_binding() -> None:
    """Evidence carrying the simulator's identity must not qualify real dispatch.

    Even when every other check/manual-attestation field is filled in as if
    the run had passed, evidence whose ``profile_digest``/``provider_id`` are
    derived from the lab simulator's own profile -- instead of the real
    production ``DispatchableBatteryBinding`` -- must fail ``qualifies``.
    """

    binding = _binding()
    simulator = BatterySimulator(_lab_simulation_profile())
    lab_digest = "sha256:" + hashlib.sha256(
        repr(simulator.profile).encode("utf-8")
    ).hexdigest()
    now = datetime(2026, 8, 26, 12, tzinfo=UTC)
    evidence = BatteryHILEvidence(
        status="passed",
        profile_digest=lab_digest,
        hardware_id="lab-simulator-inverter",
        firmware_version="simulated",
        completed_at=now,
        checks={check: True for check in REQUIRED_HIL_CHECKS},
        run_id="hil-lab-simulator-cannot-qualify",
        provider_id=simulator.profile.provider_id,
        runtime_binding_digest=lab_digest,
        takeover_evidence_digest="sha256:" + "a" * 64,
        hardware_identity_observed=True,
        firmware_identity_observed=True,
        manual_check_status={check: "verified" for check in MANUAL_HIL_CHECKS},
        qualification_expires_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
    )

    assert simulator.profile.provider_id != binding.provider_id
    assert evidence.qualifies(binding, now=now) is False
