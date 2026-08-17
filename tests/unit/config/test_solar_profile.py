import json
from pathlib import Path

import pytest

from domoai.config.solar_profile import (
    JsonSolarInstallationProfileSource,
    SolarProfileConfigurationError,
    resolve_solar_profile,
)


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
    }


def test_json_profile_source_loads_once_persisted_metadata(tmp_path: Path) -> None:
    path = tmp_path / "solar-profile.json"
    path.write_text(json.dumps(profile_payload()), encoding="utf-8")

    profile = JsonSolarInstallationProfileSource(path).load()

    assert profile.latitude == 40.4168
    assert profile.inverter_ac_max_kw == 5


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        {"schema_version": "v1"},
        profile_payload() | {"unknown": "field"},
    ],
)
def test_json_profile_source_fails_safely(tmp_path: Path, payload: object) -> None:
    path = tmp_path / "solar-profile.json"
    path.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(SolarProfileConfigurationError, match="solar profile") as raised:
        JsonSolarInstallationProfileSource(path).load()
    assert "not-json" not in str(raised.value)


def test_resolver_rejects_ambiguous_profile_sources(tmp_path: Path) -> None:
    path = tmp_path / "solar-profile.json"
    path.write_text(json.dumps(profile_payload()), encoding="utf-8")

    with pytest.raises(SolarProfileConfigurationError, match="cannot be combined"):
        resolve_solar_profile(
            profile_path=path,
            latitude=40.4168,
            longitude=None,
            installed_kwp=None,
            tilt=None,
            azimuth=None,
            performance_ratio=None,
            inverter_ac_max_kw=None,
            timezone="Europe/Madrid",
        )


def test_resolver_legacy_values_create_same_profile_without_file() -> None:
    profile = resolve_solar_profile(
        profile_path=None,
        latitude=40.4168,
        longitude=-3.7038,
        installed_kwp=6,
        tilt=30,
        azimuth=0,
        performance_ratio=0.82,
        inverter_ac_max_kw=5,
        timezone="Europe/Madrid",
    )

    assert profile.source_id == "operator_config"
    assert profile.inverter_ac_max_kw == 5


def test_resolver_does_not_invent_missing_critical_values() -> None:
    with pytest.raises(SolarProfileConfigurationError, match="DOMOAI_SOLAR_LAT"):
        resolve_solar_profile(
            profile_path=None,
            latitude=None,
            longitude=-3.7038,
            installed_kwp=6,
            tilt=30,
            azimuth=0,
            performance_ratio=0.82,
            inverter_ac_max_kw=None,
            timezone="Europe/Madrid",
        )
