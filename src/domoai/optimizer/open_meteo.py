"""Read-only Open-Meteo solar forecast provider.

The adapter converts public irradiance data into canonical household power.
It owns HTTP, timestamp normalization, installation assumptions and source
diagnostics; the optimizer only sees ``SolarForecastSeries``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import Field, model_validator

from domoai.domain.models import StrictModel
from domoai.domain.solar import SolarInstallationProfile
from domoai.optimizer.energy import SolarForecastPoint
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.providers import (
    EnergyProviderDiagnostic,
    EnergyProviderError,
    SolarForecastSeries,
)

OPEN_METEO_BASE_URL = "https://api.open-meteo.com"
OPEN_METEO_SOURCE_ID = "open_meteo_solar"
_SAFE_REVISION = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"


class OpenMeteoSolarConfig(StrictModel):
    """Explicit assumptions needed to turn irradiance into AC power."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    installed_kwp: float = Field(gt=0)
    tilt: float = Field(ge=0, le=90)
    azimuth: float = Field(ge=-180, le=180)
    performance_ratio: float = Field(gt=0, le=1)
    inverter_ac_max_kw: float | None = Field(default=None, gt=0)
    timezone: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timezone(self) -> OpenMeteoSolarConfig:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return self

    @classmethod
    def from_profile(cls, profile: SolarInstallationProfile) -> OpenMeteoSolarConfig:
        """Convert canonical installation metadata into provider assumptions."""

        return cls(
            latitude=profile.latitude,
            longitude=profile.longitude,
            installed_kwp=profile.installed_kwp,
            tilt=profile.tilt,
            azimuth=profile.azimuth,
            performance_ratio=profile.performance_ratio,
            inverter_ac_max_kw=profile.inverter_ac_max_kw,
            timezone=profile.timezone,
        )


@dataclass(frozen=True)
class OpenMeteoForecastFile:
    """Decoded source response and safe provenance metadata."""

    payload: Mapping[str, Any]
    source_revision: str
    observed_at: datetime


class OpenMeteoForecastClient(Protocol):
    """Source boundary used by ``OpenMeteoSolarProvider``."""

    def fetch_forecast(
        self, horizon: Horizon, config: OpenMeteoSolarConfig
    ) -> OpenMeteoForecastFile: ...


class OpenMeteoHttpClient:
    """HTTP client for Open-Meteo's public forecast endpoint."""

    def __init__(
        self,
        *,
        base_url: str = OPEN_METEO_BASE_URL,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            follow_redirects=True,
        )

    def fetch_forecast(
        self, horizon: Horizon, config: OpenMeteoSolarConfig
    ) -> OpenMeteoForecastFile:
        local_start = horizon.start.astimezone(ZoneInfo(config.timezone))
        local_end = horizon.end.astimezone(ZoneInfo(config.timezone))
        response = self._client.get(
            "/v1/forecast",
            params={
                "latitude": config.latitude,
                "longitude": config.longitude,
                "minutely_15": "global_tilted_irradiance",
                "tilt": config.tilt,
                "azimuth": config.azimuth,
                "timezone": config.timezone,
                "timeformat": "unixtime",
                "start_date": local_start.date().isoformat(),
                "end_date": local_end.date().isoformat(),
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Open-Meteo response is not a JSON object")
        if not payload:
            raise ValueError("Open-Meteo response is empty")
        revision = f"forecast:{config.timezone}:{local_start.date().isoformat()}"
        return OpenMeteoForecastFile(
            payload=payload,
            source_revision=revision,
            observed_at=datetime.now(UTC),
        )

    def close(self) -> None:
        self._client.close()


def _provider_error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, str | int | float | bool] | None = None,
) -> EnergyProviderError:
    return EnergyProviderError(
        EnergyProviderDiagnostic(
            code=code,
            provider_id=OPEN_METEO_SOURCE_ID,
            message=message,
            retryable=retryable,
            details=details or {},
        )
    )


def _is_retryable_transport(error: Exception) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500
    return isinstance(error, (httpx.RequestError, ConnectionError, TimeoutError, OSError))


def _as_sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"Open-Meteo {name} must be an array")
    return value


def _timestamp_key(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Open-Meteo timestamps must be numeric UNIX seconds")
    timestamp = float(value)
    if not math.isfinite(timestamp) or timestamp % (15 * 60) != 0:
        raise ValueError("Open-Meteo timestamps must be finite 15-minute UNIX instants")
    return int(timestamp)


def parse_open_meteo_solar_forecast(
    payload: Mapping[str, Any],
    *,
    horizon: Horizon,
    config: OpenMeteoSolarConfig,
) -> list[SolarForecastPoint]:
    """Map Open-Meteo UNIX timestamps to exact UTC horizon slots."""

    if payload.get("timezone") != config.timezone:
        raise ValueError("Open-Meteo response timezone does not match configuration")
    minutely = payload.get("minutely_15")
    if not isinstance(minutely, Mapping):
        raise ValueError("Open-Meteo response has no minutely_15 data")
    timestamps = _as_sequence(minutely.get("time"), "timestamps")
    irradiance = _as_sequence(
        minutely.get("global_tilted_irradiance"), "global tilted irradiance"
    )
    if len(timestamps) != len(irradiance):
        raise ValueError("Open-Meteo timestamp and irradiance arrays differ in length")

    values: dict[int, object] = {}
    for timestamp, value in zip(timestamps, irradiance, strict=True):
        key = _timestamp_key(timestamp)
        if key in values:
            raise ValueError("Open-Meteo contains duplicate UTC instants")
        values[key] = value

    horizon_start = horizon.start.astimezone(UTC)
    points: list[SolarForecastPoint] = []
    for slot in range(horizon.slots):
        expected = int(horizon_start.timestamp() + slot * horizon.resolution_minutes * 60)
        if expected not in values:
            raise ValueError("Open-Meteo response does not cover the requested horizon")
        raw_irradiance = values[expected]
        if isinstance(raw_irradiance, bool) or not isinstance(raw_irradiance, (int, float)):
            raise ValueError("Open-Meteo irradiance must be numeric")
        irradiance_wm2 = float(raw_irradiance)
        if not math.isfinite(irradiance_wm2) or irradiance_wm2 < 0:
            raise ValueError("Open-Meteo irradiance must be finite and non-negative")
        power_kw = (
            config.installed_kwp
            * irradiance_wm2
            / 1000
            * config.performance_ratio
        )
        if config.inverter_ac_max_kw is not None:
            power_kw = min(power_kw, config.inverter_ac_max_kw)
        if not math.isfinite(power_kw) or power_kw < 0:
            raise ValueError("Open-Meteo converted power is invalid")
        points.append(SolarForecastPoint(slot=slot, power=power_kw))
    return points


class OpenMeteoSolarProvider:
    """Host-injected Open-Meteo implementation of ``SolarForecastProvider``."""

    provider_id = OPEN_METEO_SOURCE_ID

    def __init__(self, client: OpenMeteoForecastClient, config: OpenMeteoSolarConfig) -> None:
        self.client = client
        self.config = config

    def get_forecast(self, horizon: Horizon) -> SolarForecastSeries:
        self._validate_horizon(horizon)
        try:
            downloaded = self.client.fetch_forecast(horizon, self.config)
        except EnergyProviderError:
            raise
        except ValueError as error:
            raise _provider_error(
                "provider_invalid", "Open-Meteo response is invalid"
            ) from error
        except Exception as error:
            raise _provider_error(
                "provider_unavailable",
                "Open-Meteo solar source is unavailable",
                retryable=_is_retryable_transport(error),
            ) from error

        if not isinstance(downloaded, OpenMeteoForecastFile):
            raise _provider_error(
                "provider_invalid", "Open-Meteo client returned an invalid forecast"
            )
        if not isinstance(downloaded.source_revision, str) or not re.fullmatch(
            _SAFE_REVISION, downloaded.source_revision
        ):
            raise _provider_error(
                "provider_invalid", "Open-Meteo source revision is invalid"
            )
        if downloaded.observed_at.tzinfo is None:
            raise _provider_error(
                "provider_invalid", "Open-Meteo observation is not timezone-aware"
            )
        try:
            points = parse_open_meteo_solar_forecast(
                downloaded.payload,
                horizon=horizon,
                config=self.config,
            )
            return SolarForecastSeries(
                horizon=horizon,
                source_id=self.provider_id,
                source_revision=downloaded.source_revision,
                observed_at=downloaded.observed_at,
                points=points,
            )
        except (TypeError, ValueError) as error:
            raise _provider_error(
                "provider_invalid",
                "Open-Meteo solar forecast is invalid",
                details={"timezone": self.config.timezone},
            ) from error

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def _validate_horizon(self, horizon: Horizon) -> None:
        if horizon.timezone != self.config.timezone or horizon.resolution_minutes != 15:
            raise _provider_error(
                "unsupported_horizon",
                "Open-Meteo v1 requires the configured timezone and 15-minute resolution",
            )


__all__ = [
    "OPEN_METEO_BASE_URL",
    "OPEN_METEO_SOURCE_ID",
    "OpenMeteoForecastFile",
    "OpenMeteoForecastClient",
    "OpenMeteoHttpClient",
    "OpenMeteoSolarConfig",
    "OpenMeteoSolarProvider",
    "parse_open_meteo_solar_forecast",
]
