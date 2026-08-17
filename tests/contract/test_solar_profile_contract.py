import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.domain.solar import SolarInstallationProfile

ROOT = Path(__file__).resolve().parents[2]


def profile_payload() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "profile_id": "home",
        "latitude": 40.4168,
        "longitude": -3.7038,
        "installed_kwp": 6.0,
        "tilt": 30.0,
        "azimuth": 0.0,
        "performance_ratio": 0.82,
        "inverter_ac_max_kw": 5.0,
        "timezone": "Europe/Madrid",
        "source_id": "operator_config",
        "source_revision": "2026-08-16",
        "captured_at": datetime(2026, 8, 16, 12, tzinfo=UTC).isoformat(),
    }


def test_solar_profile_round_trips_as_versioned_strict_contract() -> None:
    profile = SolarInstallationProfile.model_validate(profile_payload())

    assert profile.schema_version == "v1"
    assert profile.installed_kwp == 6
    assert profile.captured_at is not None
    assert profile.captured_at.tzinfo is not None

    with pytest.raises(ValidationError):
        SolarInstallationProfile.model_validate(profile_payload() | {"unexpected": True})


def test_solar_profile_rejects_naive_provenance_timestamp() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        SolarInstallationProfile.model_validate(
            profile_payload() | {"captured_at": "2026-08-16T12:00:00"}
        )


def test_solar_profile_schema_is_published_and_versioned() -> None:
    schema = json.loads(
        (ROOT / "schemas" / "v1" / "solar-installation-profile.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["properties"]["schema_version"]["const"] == "v1"
    assert "latitude" in schema["required"]
