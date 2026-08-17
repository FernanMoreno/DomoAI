"""Safe sources and resolution for persisted solar installation metadata."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from domoai.domain.solar import SolarInstallationProfile


class SolarProfileConfigurationError(ValueError):
    """Safe configuration failure without source contents or exception text."""


class SolarInstallationProfileSource(Protocol):
    """Replaceable source boundary for local or future discovered profiles."""

    def load(self) -> SolarInstallationProfile: ...


class JsonSolarInstallationProfileSource:
    """Load one strict, local, credential-free JSON profile."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> SolarInstallationProfile:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SolarProfileConfigurationError(
                "solar profile file is unavailable or not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise SolarProfileConfigurationError("solar profile must be a JSON object")
        try:
            return SolarInstallationProfile.model_validate(payload)
        except ValidationError as error:
            raise SolarProfileConfigurationError("solar profile does not satisfy v1") from error


def resolve_solar_profile(
    *,
    profile_path: Path | None,
    latitude: float | None,
    longitude: float | None,
    installed_kwp: float | None,
    tilt: float | None,
    azimuth: float | None,
    performance_ratio: float | None,
    inverter_ac_max_kw: float | None,
    timezone: str,
) -> SolarInstallationProfile:
    """Resolve the sole configured source, rejecting ambiguity and omissions."""

    legacy_values = {
        "DOMOAI_SOLAR_LAT": latitude,
        "DOMOAI_SOLAR_LON": longitude,
        "DOMOAI_SOLAR_KWP": installed_kwp,
        "DOMOAI_SOLAR_TILT": tilt,
        "DOMOAI_SOLAR_AZIMUTH": azimuth,
        "DOMOAI_SOLAR_PERFORMANCE_RATIO": performance_ratio,
        "DOMOAI_SOLAR_INVERTER_AC_MAX_KW": inverter_ac_max_kw,
    }
    if profile_path is not None:
        configured_legacy = [name for name, value in legacy_values.items() if value is not None]
        if configured_legacy:
            raise SolarProfileConfigurationError(
                "DOMOAI_SOLAR_PROFILE_PATH cannot be combined with legacy solar fields"
            )
        return JsonSolarInstallationProfileSource(profile_path).load()

    required = {
        name: value
        for name, value in legacy_values.items()
        if name != "DOMOAI_SOLAR_INVERTER_AC_MAX_KW"
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise SolarProfileConfigurationError(
            "missing live solar configuration: " + ", ".join(missing)
        )
    assert latitude is not None
    assert longitude is not None
    assert installed_kwp is not None
    assert tilt is not None
    assert azimuth is not None
    assert performance_ratio is not None
    return SolarInstallationProfile(
        latitude=latitude,
        longitude=longitude,
        installed_kwp=installed_kwp,
        tilt=tilt,
        azimuth=azimuth,
        performance_ratio=performance_ratio,
        inverter_ac_max_kw=inverter_ac_max_kw,
        timezone=timezone,
    )


__all__ = [
    "JsonSolarInstallationProfileSource",
    "SolarInstallationProfileSource",
    "SolarProfileConfigurationError",
    "resolve_solar_profile",
]
