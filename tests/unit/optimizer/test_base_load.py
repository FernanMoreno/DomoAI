"""Base-demand provenance and validation at provider/context boundaries."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from domoai.optimizer import providers
from domoai.optimizer.energy import BaseLoadPoint, EnergyContext
from domoai.optimizer.providers import (
    ComposedEnergyContextProvider,
    EnergyProviderError,
    StaticSolarForecastProvider,
    StaticTariffProvider,
)
from tests.fixtures.base_load import NOW, horizon, solar, tariffs


def base_series(**updates):
    assert hasattr(providers, "BaseLoadForecastSeries"), (
        "base forecast provider contract is missing"
    )
    return providers.BaseLoadForecastSeries(
        **{
            "horizon": horizon(),
            "source_id": "base-fixture",
            "source_revision": "r1",
            "observed_at": NOW,
            "kind": "fixture",
            "scope": "uncontrolled_demand",
            "points": [BaseLoadPoint(slot=i, power=1) for i in range(horizon().slots)],
            **updates,
        }
    )


class BaseProvider:
    provider_id = "base-fixture"

    def __init__(self, series=None, error=None):
        self.series = series
        self.error = error

    def get_forecast(self, requested):
        if self.error:
            raise self.error
        return self.series


def composer(base):
    return ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs(horizon())),
        StaticSolarForecastProvider(solar(horizon())),
        base_load=base,
        now=lambda: NOW,
    )


@pytest.mark.parametrize(
    "points",
    [
        [],
        [BaseLoadPoint(slot=i, power=1) for i in range(7)],
        [BaseLoadPoint(slot=0, power=1)] * 8,
        [BaseLoadPoint(slot=i, power=1) for i in reversed(range(8))],
        [
            BaseLoadPoint(slot=i, power=1, confidence={"low": 0, "high": 2} if i == 0 else None)
            for i in range(8)
        ],
    ],
)
def test_series_rejects_gaps_duplicates_order_and_partial_bands(points) -> None:
    with pytest.raises(ValueError):
        base_series(points=points)


@pytest.mark.parametrize(
    "payload",
    [
        {"power": float("inf")},
        {"power": float("nan")},
        {"power": -1},
        {"power": 1, "unit": "W"},
        {"power": 1, "unit": "kWh"},
        {"power": 1, "confidence": {"low": 0, "high": float("inf")}},
    ],
)
def test_base_points_reject_noncanonical_values(payload) -> None:
    with pytest.raises(ValidationError):
        BaseLoadPoint(slot=0, **payload)


@pytest.mark.parametrize(
    "updates",
    [
        {"scope": "net_meter"},
        {"scope": "whole_home"},
        {"kind": "instantaneous_meter"},
        {"kind": "declared_reserve"},
        {"observed_at": NOW.replace(tzinfo=None)},
    ],
)
def test_series_rejects_contaminated_or_incomplete_provenance(updates) -> None:
    with pytest.raises(ValueError):
        base_series(**updates)


@pytest.mark.parametrize("delta,code", [(-901, "stale_provider_data"), (1, "invalid_observed_at")])
def test_observed_forecast_freshness_remains_enforced(delta, code) -> None:
    series = base_series(kind="forecast", observed_at=NOW + timedelta(seconds=delta))
    with pytest.raises(EnergyProviderError) as failure:
        composer(BaseProvider(series)).get_context(horizon())
    assert failure.value.diagnostic.code == code


def test_composer_validates_horizon_and_sanitizes_provider_failure() -> None:
    shifted = horizon().model_copy(
        update={"start": NOW + timedelta(hours=1), "end": NOW + timedelta(hours=3)}
    )
    with pytest.raises(EnergyProviderError) as mismatch:
        composer(BaseProvider(base_series(horizon=shifted))).get_context(horizon())
    assert mismatch.value.diagnostic.code == "horizon_mismatch"
    with pytest.raises(EnergyProviderError) as failure:
        composer(BaseProvider(error=TimeoutError("private-token"))).get_context(horizon())
    assert failure.value.diagnostic.retryable is True
    assert "private-token" not in str(failure.value)
    with pytest.raises(EnergyProviderError) as invalid:
        composer(BaseProvider(series={})).get_context(horizon())
    assert invalid.value.diagnostic.code == "provider_invalid"


def test_fixture_zero_and_legacy_missing_have_distinct_evidence() -> None:
    from domoai.optimizer.cp_sat import CpSatOptimizer
    from domoai.optimizer.scenario import OptimizationScenario
    from domoai.runtime.registry import DeviceRegistry

    zero = base_series(points=[BaseLoadPoint(slot=i, power=0) for i in range(8)])
    context = composer(BaseProvider(zero)).get_context(horizon())
    explicit = CpSatOptimizer(DeviceRegistry()).optimize(
        OptimizationScenario(id="fixture-zero", horizon=horizon(), energy_context=context)
    )
    provenance = explicit.constraint_summary["forecast_assumptions"]["base_load"]
    assert provenance["kind"] == "fixture"
    assert provenance["all_zero"] is True
    assert context.base_load_source.source_id == "base-fixture"
    without = context.model_dump()
    without.pop("base_load_source")
    without["schema_version"] = "v1"
    legacy = EnergyContext.model_validate(without)
    result = CpSatOptimizer(DeviceRegistry()).optimize(
        OptimizationScenario(id="legacy-zero", horizon=horizon(), energy_context=legacy)
    )
    assert result.constraint_summary["forecast_assumptions"]["base_load"]["kind"] == "unspecified"
    without["base_load_forecast"] = None
    unknown = EnergyContext.model_validate(without)
    result = CpSatOptimizer(DeviceRegistry()).optimize(
        OptimizationScenario(id="unknown", horizon=horizon(), energy_context=unknown)
    )
    assert result.constraint_summary["forecast_assumptions"]["base_load"]["status"] == "unknown"


def test_context_cannot_claim_base_provenance_without_points() -> None:
    context = composer(BaseProvider(base_series())).get_context(horizon()).model_dump()
    context["base_load_forecast"] = None
    with pytest.raises(ValueError):
        EnergyContext.model_validate(context)


def invalid_returned_series(case):
    series = base_series()
    if case == "mutated_unit":
        series.points[0].unit = "W"
    elif case == "copied_unit":
        series.points[0] = series.points[0].model_copy(update={"unit": "W"})
    elif case == "copied_infinity":
        series = series.model_copy(
            update={
                "points": [
                    point.model_copy(update={"power": float("inf")}) for point in series.points
                ]
            }
        )
    elif case == "copied_source":
        series = series.model_copy(update={"source_id": "private-token / invalid"})
    elif case == "copied_scope":
        series = series.model_copy(update={"scope": "net_meter"})
    return series


@pytest.mark.parametrize(
    "case", ["mutated_unit", "copied_unit", "copied_infinity", "copied_source", "copied_scope"]
)
def test_composer_revalidates_mutated_or_copied_base_result(case) -> None:
    with pytest.raises(EnergyProviderError) as failure:
        composer(BaseProvider(invalid_returned_series(case))).get_context(horizon())
    assert failure.value.diagnostic.code == "provider_invalid"
    assert failure.value.diagnostic.retryable is False
    assert "private-token" not in failure.value.diagnostic.model_dump_json()


def test_composed_base_snapshot_is_detached_from_valid_provider_result() -> None:
    series = base_series()
    context = composer(BaseProvider(series)).get_context(horizon())
    before = context.model_dump(mode="json")
    series.points[0].power = 9.0
    series.horizon.resolution_minutes = 30
    series.source_revision = "changed-after-return"
    assert context.model_dump(mode="json") == before
    assert context.base_load_forecast is not series.points
    assert context.base_load_forecast[0] is not series.points[0]
