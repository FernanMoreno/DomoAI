"""Composable, read-only providers for canonical energy context data.

The provider boundary deliberately contains no HTTP, MQTT, protocol, or
credential concerns. Concrete integrations translate their source data into
these versioned models, while the composer performs cross-provider validation
before exposing the existing :class:`EnergyContextProvider` port.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from domoai.domain.models import StrictModel
from domoai.optimizer.energy import (
    BatteryProfile,
    EnergyContext,
    SolarForecastPoint,
    TariffPoint,
)
from domoai.optimizer.horizon import Horizon


def _validate_slots(
    points: list[TariffPoint] | list[SolarForecastPoint],
    horizon: Horizon,
    series_name: str,
) -> None:
    expected = list(range(horizon.slots))
    slots = [point.slot for point in points]
    if len(points) != horizon.slots:
        raise ValueError(f"{series_name} must contain exactly one point for every horizon slot")
    if len(set(slots)) != len(slots):
        raise ValueError(f"{series_name} must not contain duplicate slots")
    if slots != expected:
        if sorted(slots) == expected:
            raise ValueError(f"{series_name} slots must be ordered")
        raise ValueError(f"{series_name} slots must cover the horizon exactly")


class ProviderMetadata(StrictModel):
    """Provenance shared by all provider results, safe for diagnostics."""

    schema_version: Literal["v1"] = "v1"
    horizon: Horizon
    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    source_revision: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", max_length=128
    )
    observed_at: datetime

    @model_validator(mode="after")
    def validate_timestamp(self) -> ProviderMetadata:
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


class TariffSeries(ProviderMetadata):
    """One canonical tariff point per slot in the requested horizon."""

    points: list[TariffPoint]

    @model_validator(mode="after")
    def validate_points(self) -> TariffSeries:
        _validate_slots(self.points, self.horizon, "tariff points")
        return self


class SolarForecastSeries(ProviderMetadata):
    """One canonical solar power forecast point per slot."""

    points: list[SolarForecastPoint]

    @model_validator(mode="after")
    def validate_points(self) -> SolarForecastSeries:
        _validate_slots(self.points, self.horizon, "solar forecast points")
        return self


class BatteryState(ProviderMetadata):
    """Battery state for the horizon; ``None`` means no battery is available."""

    battery: BatteryProfile | None = None


class EnergyProviderDiagnostic(StrictModel):
    """Sanitized, stable error information exposed across the provider boundary."""

    schema_version: Literal["v1"] = "v1"
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$", max_length=64)
    message: str = Field(min_length=1, max_length=240)
    retryable: bool
    details: dict[str, str | int | float | bool] = Field(default_factory=dict)


class EnergyProviderError(ValueError):
    """Typed provider failure whose public text contains no source exception."""

    def __init__(self, diagnostic: EnergyProviderDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


class TariffProvider(Protocol):
    """Read-only source of canonical tariff data."""

    provider_id: str

    def get_tariffs(self, horizon: Horizon) -> TariffSeries: ...


class SolarForecastProvider(Protocol):
    """Read-only source of canonical solar forecasts."""

    provider_id: str

    def get_forecast(self, horizon: Horizon) -> SolarForecastSeries: ...


class BatteryProvider(Protocol):
    """Read-only source of canonical battery state."""

    provider_id: str

    def get_state(self, horizon: Horizon) -> BatteryState: ...


def _safe_provider_id(provider: object, fallback: str) -> str:
    candidate = getattr(provider, "provider_id", fallback)
    if isinstance(candidate, str) and candidate:
        try:
            return EnergyProviderDiagnostic(
                code="provider_unavailable",
                provider_id=candidate,
                message="provider unavailable",
                retryable=False,
            ).provider_id
        except ValueError:
            pass
    return fallback


def _provider_error(
    provider_id: str,
    *,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, str | int | float | bool] | None = None,
) -> EnergyProviderError:
    return EnergyProviderError(
        EnergyProviderDiagnostic(
            code=code,
            provider_id=provider_id,
            message=message,
            retryable=retryable,
            details=details or {},
        )
    )


def _is_retryable_exception(error: Exception) -> bool:
    return isinstance(error, (ConnectionError, TimeoutError, OSError))


class ComposedEnergyContextProvider:
    """Compose tariff, solar, and optional battery sources into one context."""

    def __init__(
        self,
        tariffs: TariffProvider,
        solar: SolarForecastProvider,
        battery: BatteryProvider | None = None,
        export_tariffs: TariffProvider | None = None,
        *,
        max_age_seconds: float | None = 900,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if max_age_seconds is not None and max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative or None")
        self.tariffs = tariffs
        self.solar = solar
        self.battery = battery
        self.export_tariffs = export_tariffs
        self.max_age_seconds = max_age_seconds
        self._now = now or (lambda: datetime.now(UTC))

    def get_context(self, horizon: Horizon) -> EnergyContext:
        tariff_result = self._get_tariffs(horizon)
        solar_result = self._get_solar(horizon)
        battery_result = self._get_battery(horizon) if self.battery is not None else None
        export_tariff_result = (
            self._get_export_tariffs(horizon) if self.export_tariffs is not None else None
        )

        self._validate_result_horizon(tariff_result, horizon, "tariff")
        self._validate_result_horizon(solar_result, horizon, "solar")
        if battery_result is not None:
            self._validate_result_horizon(battery_result, horizon, "battery")
        if export_tariff_result is not None:
            self._validate_result_horizon(export_tariff_result, horizon, "export_tariff")

        results = [tariff_result, solar_result]
        if battery_result is not None:
            results.append(battery_result)
        if export_tariff_result is not None:
            results.append(export_tariff_result)
        for result in results:
            self._validate_freshness(result)

        battery_revision = (
            f"{battery_result.source_id}@{battery_result.source_revision}"
            if battery_result is not None
            else "none"
        )
        revision_parts = [
            f"tariff:{tariff_result.source_id}@{tariff_result.source_revision}",
            f"solar:{solar_result.source_id}@{solar_result.source_revision}",
            f"battery:{battery_revision}",
        ]
        if export_tariff_result is not None:
            revision_parts.append(
                f"export_tariff:{export_tariff_result.source_id}"
                f"@{export_tariff_result.source_revision}"
            )
        source_revision = "|".join(revision_parts)
        return EnergyContext(
            horizon=horizon,
            tariffs=tariff_result.points,
            solar_forecast=solar_result.points,
            battery=battery_result.battery if battery_result is not None else None,
            export_tariffs=(
                export_tariff_result.points if export_tariff_result is not None else None
            ),
            source_revision=source_revision,
            observed_at=min(result.observed_at for result in results),
        )

    def _get_tariffs(self, horizon: Horizon) -> TariffSeries:
        return self._get_tariff_series(self.tariffs, horizon, "tariff")

    def _get_export_tariffs(self, horizon: Horizon) -> TariffSeries:
        assert self.export_tariffs is not None
        return self._get_tariff_series(self.export_tariffs, horizon, "export_tariff")

    def _get_tariff_series(
        self, provider: TariffProvider, horizon: Horizon, label: str
    ) -> TariffSeries:
        provider_id = _safe_provider_id(provider, label)
        try:
            result = provider.get_tariffs(horizon)
        except EnergyProviderError:
            raise
        except Exception as error:
            raise _provider_error(
                provider_id,
                code=(
                    "provider_unavailable"
                    if _is_retryable_exception(error)
                    else "provider_invalid"
                ),
                message=f"{label} provider failed",
                retryable=_is_retryable_exception(error),
            ) from error
        if not isinstance(result, TariffSeries):
            raise _provider_error(
                provider_id,
                code="provider_invalid",
                message=f"{label} provider returned an invalid result",
            )
        return result

    def _get_solar(self, horizon: Horizon) -> SolarForecastSeries:
        provider_id = _safe_provider_id(self.solar, "solar")
        try:
            result = self.solar.get_forecast(horizon)
        except EnergyProviderError:
            raise
        except Exception as error:
            raise _provider_error(
                provider_id,
                code=(
                    "provider_unavailable"
                    if _is_retryable_exception(error)
                    else "provider_invalid"
                ),
                message="solar provider failed",
                retryable=_is_retryable_exception(error),
            ) from error
        if not isinstance(result, SolarForecastSeries):
            raise _provider_error(
                provider_id,
                code="provider_invalid",
                message="solar provider returned an invalid result",
            )
        return result

    def _get_battery(self, horizon: Horizon) -> BatteryState:
        assert self.battery is not None
        provider_id = _safe_provider_id(self.battery, "battery")
        try:
            result = self.battery.get_state(horizon)
        except EnergyProviderError:
            raise
        except Exception as error:
            raise _provider_error(
                provider_id,
                code=(
                    "provider_unavailable"
                    if _is_retryable_exception(error)
                    else "provider_invalid"
                ),
                message="battery provider failed",
                retryable=_is_retryable_exception(error),
            ) from error
        if not isinstance(result, BatteryState):
            raise _provider_error(
                provider_id,
                code="provider_invalid",
                message="battery provider returned an invalid result",
            )
        return result

    def _validate_result_horizon(
        self, result: ProviderMetadata, requested: Horizon, kind: str
    ) -> None:
        if result.horizon != requested:
            raise _provider_error(
                result.source_id,
                code="horizon_mismatch",
                message=f"{kind} provider returned a different horizon",
            )

    def _validate_freshness(self, result: ProviderMetadata) -> None:
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("provider freshness clock must be timezone-aware")
        age_seconds = (current - result.observed_at).total_seconds()
        if age_seconds < 0:
            raise _provider_error(
                result.source_id,
                code="invalid_observed_at",
                message="provider observation is in the future",
            )
        if self.max_age_seconds is not None and age_seconds > self.max_age_seconds:
            raise _provider_error(
                result.source_id,
                code="stale_provider_data",
                message="provider data is stale",
                details={
                    "age_seconds": round(age_seconds, 3),
                    "max_age_seconds": self.max_age_seconds,
                },
            )


class StaticTariffProvider:
    """Deterministic tariff provider for fixtures and local deployments."""

    def __init__(self, series: TariffSeries) -> None:
        self._series = series
        self.provider_id = series.source_id

    def get_tariffs(self, horizon: Horizon) -> TariffSeries:
        if self._series.horizon != horizon:
            raise _provider_error(
                self.provider_id,
                code="horizon_mismatch",
                message="tariff provider horizon does not match request",
            )
        return self._series


class StaticSolarForecastProvider:
    """Deterministic solar provider for fixtures and local deployments."""

    def __init__(self, series: SolarForecastSeries) -> None:
        self._series = series
        self.provider_id = series.source_id

    def get_forecast(self, horizon: Horizon) -> SolarForecastSeries:
        if self._series.horizon != horizon:
            raise _provider_error(
                self.provider_id,
                code="horizon_mismatch",
                message="solar provider horizon does not match request",
            )
        return self._series


class StaticBatteryProvider:
    """Deterministic battery provider for fixtures and local deployments."""

    def __init__(self, state: BatteryState) -> None:
        self._state = state
        self.provider_id = state.source_id

    def get_state(self, horizon: Horizon) -> BatteryState:
        if self._state.horizon != horizon:
            raise _provider_error(
                self.provider_id,
                code="horizon_mismatch",
                message="battery provider horizon does not match request",
            )
        return self._state


__all__ = [
    "BatteryProvider",
    "BatteryState",
    "ComposedEnergyContextProvider",
    "EnergyProviderDiagnostic",
    "EnergyProviderError",
    "ProviderMetadata",
    "SolarForecastProvider",
    "SolarForecastSeries",
    "StaticBatteryProvider",
    "StaticSolarForecastProvider",
    "StaticTariffProvider",
    "TariffProvider",
    "TariffSeries",
]
