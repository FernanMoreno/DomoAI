from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from domoai.adapters.fixtures.simulated_home import SimulatedHomeAdapter
from domoai.application.runtime_factory import build_runtime
from domoai.config.battery_qualification import (
    REQUIRED_HIL_CHECKS,
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
                "completed_at": datetime(2026, 8, 23, 12, tzinfo=UTC).isoformat(),
                "checks": {check: True for check in REQUIRED_HIL_CHECKS},
                "run_id": "hil-run-1",
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
