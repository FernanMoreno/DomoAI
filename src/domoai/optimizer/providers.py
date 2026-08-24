"""Composable, read-only providers for canonical energy context data.

The provider boundary deliberately contains no HTTP, MQTT, protocol, or
credential concerns. Concrete integrations translate their source data into
these versioned models, while the composer performs cross-provider validation
before exposing the existing :class:`EnergyContextProvider` port.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from domoai.domain.energy import (
    SOC_OBSERVATION_TOLERANCE_KWH,
    BatteryCapacityEvidence,
    BatteryProfile,
    BatterySocConversionEvidence,
    BatterySocObservation,
    DispatchableBatteryBinding,
    NominalCapacityTrustPolicy,
)
from domoai.domain.models import StateSnapshot, StateStatus, StrictModel
from domoai.domain.provider import Measurement, MeasurementQuality
from domoai.optimizer.energy import (
    EnergyContext,
    SolarForecastPoint,
    TariffPoint,
)
from domoai.optimizer.horizon import Horizon
from domoai.runtime.clock import SystemClock
from domoai.runtime.ports import StateStorePort


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
    source_revision: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$", max_length=128)
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

    @model_validator(mode="after")
    def validate_dispatchable_soc(self) -> BatteryState:
        if self.battery is None or self.battery.actuator is None:
            return self
        observation = self.battery.initial_soc_observation
        if observation is None:
            raise ValueError("dispatchable battery requires an initial SOC observation")
        if observation.quality is not MeasurementQuality.GOOD:
            raise ValueError("dispatchable battery SOC observation must have GOOD quality")
        if observation.provider_id != self.source_id:
            raise ValueError("dispatchable battery SOC observation provider must match source_id")
        if observation.device_id != self.battery.actuator.device_id:
            raise ValueError("dispatchable battery SOC observation device must match actuator")
        if not math.isclose(
            observation.value_kwh,
            self.battery.initial_soc_kwh,
            rel_tol=0.0,
            abs_tol=SOC_OBSERVATION_TOLERANCE_KWH,
        ):
            raise ValueError(
                "dispatchable battery SOC observation must match initial_soc_kwh"
            )
        return self


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


def validate_nominal_capacity_trust(
    evidence: BatteryCapacityEvidence,
    policy: NominalCapacityTrustPolicy | None,
) -> None:
    """Enforce server-owned trust for provider-measured capacity evidence."""

    if evidence.capacity_source == "provider_config":
        return
    if policy is None:
        raise _provider_error(
            evidence.provider_id,
            code="nominal_capacity_trust_required",
            message="provider-measured battery capacity requires server trust policy",
        )
    attestation = evidence.nominal_capacity_attestation
    if attestation is None:
        raise _provider_error(
            evidence.provider_id,
            code="nominal_capacity_trust_required",
            message="provider-measured battery capacity requires trusted attestation",
        )
    if attestation.evidence_type not in policy.allowed_evidence_types:
        raise _provider_error(
            evidence.provider_id,
            code="nominal_capacity_evidence_type_not_trusted",
            message="nominal capacity evidence type is not trusted",
        )
    if attestation.attested_by not in policy.trusted_attesters:
        raise _provider_error(
            evidence.provider_id,
            code="nominal_capacity_attester_not_trusted",
            message="nominal capacity attester is not trusted",
        )
    if attestation.reference not in policy.trusted_references:
        raise _provider_error(
            evidence.provider_id,
            code="nominal_capacity_reference_not_trusted",
            message="nominal capacity reference is not trusted",
        )


def battery_capacity_evidence_from_measurement(
    measurement: Measurement,
) -> BatteryCapacityEvidence:
    """Map one canonical measured nominal capacity into optimizer evidence."""

    if measurement.metric != "battery.capacity":
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery capacity measurement has an unsupported metric",
        )
    if measurement.unit != "kWh":
        raise _provider_error(
            measurement.provider_id,
            code="unsupported_battery_capacity_unit",
            message="battery capacity measurement must use canonical kWh units",
        )
    value = measurement.value
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery capacity measurement must be a positive finite number",
        )
    if measurement.nominal_capacity_attestation is None:
        raise _provider_error(
            measurement.provider_id,
            code="missing_nominal_capacity_attestation",
            message="measured battery capacity requires nominal capacity attestation",
        )
    try:
        return BatteryCapacityEvidence(
            provider_id=measurement.provider_id,
            device_id=measurement.device_id,
            capacity_kwh=float(value),
            capacity_source="provider_measurement",
            quality=measurement.quality,
            source_ref=measurement.source_ref,
            observed_at=measurement.observed_at,
            received_at=measurement.received_at,
            nominal_capacity_attestation=measurement.nominal_capacity_attestation,
        )
    except ValueError as error:
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery capacity measurement failed canonical validation",
        ) from error


def battery_soc_observation_from_measurement(
    measurement: Measurement,
) -> BatterySocObservation:
    """Map one canonical SDK measurement into optimizer SOC evidence."""

    if measurement.metric != "battery.soc":
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_soc_measurement",
            message="battery SOC measurement has an unsupported metric",
        )
    if measurement.unit != "kWh":
        raise _provider_error(
            measurement.provider_id,
            code="unsupported_battery_soc_unit",
            message="battery SOC measurement must use canonical kWh units",
        )
    if (
        isinstance(measurement.value, bool)
        or not isinstance(measurement.value, (int, float))
        or not math.isfinite(float(measurement.value))
    ):
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_soc_measurement",
            message="battery SOC measurement value is not a finite number",
        )
    try:
        return BatterySocObservation(
            provider_id=measurement.provider_id,
            device_id=measurement.device_id,
            metric="battery.soc",
            value_kwh=float(measurement.value),
            observed_at=measurement.observed_at,
            received_at=measurement.received_at,
            quality=measurement.quality,
            source_ref=measurement.source_ref,
        )
    except ValueError as error:
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_soc_measurement",
            message="battery SOC measurement failed canonical validation",
        ) from error


def battery_soc_observation_from_percentage_measurement(
    measurement: Measurement,
    capacity: BatteryCapacityEvidence,
) -> BatterySocObservation:
    """Convert explicit provider percentage SOC into canonical kWh evidence."""

    if measurement.metric != "battery.soc":
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_soc_percentage",
            message="battery SOC percentage has an unsupported metric",
        )
    if measurement.unit != "%":
        raise _provider_error(
            measurement.provider_id,
            code="unsupported_battery_soc_unit",
            message="battery SOC percentage must use the % unit",
        )
    if not isinstance(capacity, BatteryCapacityEvidence):
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery SOC conversion requires explicit capacity evidence",
        )
    capacity_value = capacity.capacity_kwh
    if (
        isinstance(capacity_value, bool)
        or not isinstance(capacity_value, (int, float))
        or not math.isfinite(float(capacity_value))
        or float(capacity_value) <= 0
        or capacity.capacity_source not in {"provider_config", "provider_measurement"}
    ):
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery SOC conversion capacity is invalid",
        )
    if capacity.quality is not MeasurementQuality.GOOD:
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery SOC conversion capacity quality is not good",
        )
    if (
        capacity.provider_id != measurement.provider_id
        or capacity.device_id != measurement.device_id
    ):
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_capacity",
            message="battery SOC conversion capacity identity does not match measurement",
        )
    value = measurement.value
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 100
    ):
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_soc_percentage",
            message="battery SOC percentage must be a finite value from 0 to 100",
        )
    try:
        return BatterySocObservation(
            provider_id=measurement.provider_id,
            device_id=measurement.device_id,
            metric="battery.soc",
            value_kwh=float(value) / 100.0 * float(capacity_value),
            unit="kWh",
            observed_at=measurement.observed_at,
            received_at=measurement.received_at,
            quality=measurement.quality,
            source_ref=measurement.source_ref,
            conversion_evidence=BatterySocConversionEvidence(
                source_value_percent=float(value),
                capacity=capacity,
            ),
        )
    except ValueError as error:
        raise _provider_error(
            measurement.provider_id,
            code="invalid_battery_soc_percentage",
            message="battery SOC percentage conversion failed canonical validation",
        ) from error


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
        self._now = now or SystemClock().now

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
        if battery_result is not None and battery_result.battery is not None:
            observation = battery_result.battery.initial_soc_observation
            if observation is not None:
                self._validate_observation_freshness(
                    battery_result.source_id,
                    observation.observed_at,
                )

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
                    "provider_unavailable" if _is_retryable_exception(error) else "provider_invalid"
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
                    "provider_unavailable" if _is_retryable_exception(error) else "provider_invalid"
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
                    "provider_unavailable" if _is_retryable_exception(error) else "provider_invalid"
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
        self._validate_observed_at(result.source_id, result.observed_at)

    def _validate_observation_freshness(self, source_id: str, observed_at: datetime) -> None:
        self._validate_observed_at(source_id, observed_at)

    def _validate_observed_at(self, source_id: str, observed_at: datetime) -> None:
        current = self._now()
        if current.tzinfo is None:
            raise ValueError("provider freshness clock must be timezone-aware")
        age_seconds = (current - observed_at).total_seconds()
        if age_seconds < 0:
            raise _provider_error(
                source_id,
                code="invalid_observed_at",
                message="provider observation is in the future",
            )
        if self.max_age_seconds is not None and age_seconds > self.max_age_seconds:
            raise _provider_error(
                source_id,
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


class StateStoreBatteryProvider:
    """Compose one explicit normalized SOC snapshot into provider state."""

    def __init__(
        self,
        *,
        state_store: StateStorePort,
        provider_id: str,
        device_id: str,
        soc_capability: str,
        profile: BatteryProfile,
        capacity_evidence: BatteryCapacityEvidence | None = None,
        capacity_trust_policy: NominalCapacityTrustPolicy | None = None,
    ) -> None:
        if not provider_id.strip():
            raise ValueError("provider_id must not be empty")
        if not device_id.strip():
            raise ValueError("device_id must not be empty")
        if not soc_capability.strip():
            raise ValueError("soc_capability must not be empty")
        if profile.actuator is not None and profile.actuator.device_id != device_id:
            raise ValueError("battery profile actuator device must match device_id")
        self.state_store = state_store
        self.provider_id = provider_id
        self.device_id = device_id
        self.soc_capability = soc_capability
        self.profile = profile
        self.capacity_evidence = capacity_evidence
        self.capacity_trust_policy = capacity_trust_policy

    @classmethod
    def from_binding(
        cls,
        *,
        state_store: StateStorePort,
        binding: DispatchableBatteryBinding,
    ) -> StateStoreBatteryProvider:
        """Create a provider from one complete, server-validated binding."""

        validate_nominal_capacity_trust(
            binding.capacity_evidence, binding.capacity_trust_policy
        )
        return cls(
            state_store=state_store,
            provider_id=binding.provider_id,
            device_id=binding.device_id,
            soc_capability=binding.soc_capability,
            profile=binding.profile,
            capacity_evidence=binding.capacity_evidence,
            capacity_trust_policy=binding.capacity_trust_policy,
        )

    def get_state(self, horizon: Horizon) -> BatteryState:
        snapshot = self.state_store.peek(self.device_id, self.soc_capability)
        if snapshot is None:
            raise _provider_error(
                self.provider_id,
                code="battery_soc_unavailable",
                message="battery SOC snapshot is unavailable",
            )
        if (
            snapshot.device_id != self.device_id
            or snapshot.capability != self.soc_capability
            or snapshot.source_ref.adapter_id != self.provider_id
        ):
            raise _provider_error(
                self.provider_id,
                code="battery_soc_identity_mismatch",
                message="battery SOC snapshot identity does not match binding",
            )
        if snapshot.status is StateStatus.UNAVAILABLE:
            raise _provider_error(
                self.provider_id,
                code="battery_soc_unavailable",
                message="battery SOC snapshot is unavailable",
            )
        if snapshot.status is StateStatus.INVALID:
            raise _provider_error(
                self.provider_id,
                code="battery_soc_invalid",
                message="battery SOC snapshot is invalid",
            )
        if snapshot.value is None:
            raise _provider_error(
                self.provider_id,
                code="invalid_battery_soc_measurement",
                message="battery SOC snapshot has no numeric value",
            )

        quality = (
            MeasurementQuality.GOOD
            if snapshot.status is StateStatus.CURRENT
            else MeasurementQuality.STALE
        )
        try:
            measurement = Measurement(
                provider_id=self.provider_id,
                device_id=self.device_id,
                metric=self.soc_capability,
                value=snapshot.value,
                unit=snapshot.unit,
                observed_at=snapshot.observed_at,
                received_at=snapshot.received_at,
                quality=quality,
                source_ref=snapshot.source_ref,
            )
        except ValueError as error:
            raise _provider_error(
                self.provider_id,
                code="invalid_battery_soc_measurement",
                message="battery SOC snapshot is not a valid measurement",
            ) from error

        if (
            self.profile.actuator is not None
            and self.capacity_evidence is not None
            and self.capacity_evidence.capacity_source == "provider_measurement"
        ):
            validate_nominal_capacity_trust(
                self.capacity_evidence, self.capacity_trust_policy
            )

        if measurement.unit == "kWh":
            observation = battery_soc_observation_from_measurement(measurement)
        elif measurement.unit == "%":
            if self.capacity_evidence is None:
                raise _provider_error(
                    self.provider_id,
                    code="invalid_battery_capacity",
                    message="battery SOC percentage requires explicit capacity evidence",
                )
            observation = battery_soc_observation_from_percentage_measurement(
                measurement, self.capacity_evidence
            )
        else:
            raise _provider_error(
                self.provider_id,
                code="unsupported_battery_soc_unit",
                message="battery SOC snapshot uses an unsupported unit",
            )

        try:
            profile_payload = self.profile.model_dump(mode="python")
            profile_payload["initial_soc_kwh"] = observation.value_kwh
            profile_payload["initial_soc_observation"] = observation.model_dump(mode="python")
            profile = BatteryProfile.model_validate(profile_payload)
            return BatteryState(
                horizon=horizon,
                source_id=self.provider_id,
                source_revision=self._source_revision(snapshot),
                observed_at=snapshot.observed_at,
                battery=profile,
            )
        except ValueError as error:
            raise _provider_error(
                self.provider_id,
                code="battery_state_invalid",
                message="battery SOC snapshot cannot initialize battery state",
            ) from error

    def _source_revision(self, snapshot: StateSnapshot) -> str:
        payload = {
            "provider_id": self.provider_id,
            "device_id": self.device_id,
            "soc_capability": self.soc_capability,
            "snapshot": snapshot.model_dump(mode="json"),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "BatteryProvider",
    "BatteryCapacityEvidence",
    "BatteryState",
    "DispatchableBatteryBinding",
    "battery_capacity_evidence_from_measurement",
    "battery_soc_observation_from_measurement",
    "battery_soc_observation_from_percentage_measurement",
    "ComposedEnergyContextProvider",
    "EnergyProviderDiagnostic",
    "EnergyProviderError",
    "ProviderMetadata",
    "SolarForecastProvider",
    "SolarForecastSeries",
    "StaticBatteryProvider",
    "StateStoreBatteryProvider",
    "StaticSolarForecastProvider",
    "StaticTariffProvider",
    "TariffProvider",
    "TariffSeries",
    "validate_nominal_capacity_trust",
]
