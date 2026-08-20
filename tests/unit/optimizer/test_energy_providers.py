from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from domoai.optimizer.energy import TariffPoint
from domoai.optimizer.providers import (
    BatteryState,
    ComposedEnergyContextProvider,
    EnergyProviderError,
    SolarForecastSeries,
    StaticBatteryProvider,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
)
from tests.fixtures.energy import energy_context_for

NOW = datetime(2026, 8, 15, 12, 5, tzinfo=UTC)


def provider_inputs() -> tuple[TariffSeries, SolarForecastSeries, BatteryState]:
    context = energy_context_for()
    return (
        TariffSeries(
            horizon=context.horizon,
            source_id="tariff_fixture",
            source_revision="day-ahead-1",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            points=context.tariffs,
        ),
        SolarForecastSeries(
            horizon=context.horizon,
            source_id="solar_fixture",
            source_revision="forecast-1",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            points=context.solar_forecast,
        ),
        BatteryState(
            horizon=context.horizon,
            source_id="battery_fixture",
            source_revision="state-1",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            battery=context.battery,
        ),
    )


def provider() -> ComposedEnergyContextProvider:
    tariffs, solar, battery = provider_inputs()
    return ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: NOW,
    )


def test_composer_returns_complete_context_with_deterministic_revision() -> None:
    context = provider().get_context(energy_context_for().horizon)

    assert context.battery is not None
    assert context.source_revision == (
        "tariff:tariff_fixture@day-ahead-1|"
        "solar:solar_fixture@forecast-1|"
        "battery:battery_fixture@state-1"
    )
    assert context.observed_at == datetime(2026, 8, 15, 12, tzinfo=UTC)


def test_composer_allows_optional_battery() -> None:
    tariffs, solar, _ = provider_inputs()
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        now=lambda: NOW,
    )

    context = composed.get_context(energy_context_for().horizon)

    assert context.battery is None
    assert context.source_revision.endswith("battery:none")


def test_composer_rejects_stale_provider_data_with_typed_diagnostic() -> None:
    tariffs, solar, battery = provider_inputs()
    stale = tariffs.model_copy(update={"observed_at": NOW - timedelta(seconds=61)})
    composed = ComposedEnergyContextProvider(
        StaticTariffProvider(stale),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        max_age_seconds=60,
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    assert raised.value.diagnostic.code == "stale_provider_data"
    assert raised.value.diagnostic.provider_id == "tariff_fixture"
    assert raised.value.diagnostic.details["max_age_seconds"] == 60


def test_composer_rejects_a_different_horizon() -> None:
    tariffs, solar, battery = provider_inputs()
    different_horizon = energy_context_for().horizon.model_copy(
        update={"end": energy_context_for().horizon.end + timedelta(minutes=15)}
    )

    with pytest.raises(EnergyProviderError) as raised:
        provider().get_context(different_horizon)

    assert raised.value.diagnostic.code == "horizon_mismatch"
    assert raised.value.diagnostic.provider_id == "tariff_fixture"


def test_provider_failure_is_sanitized() -> None:
    tariffs, solar, battery = provider_inputs()

    class FailingTariffs:
        provider_id = "tariff_live"

        def get_tariffs(self, _horizon: object) -> TariffSeries:
            raise RuntimeError("access_token=do-not-leak")

    composed = ComposedEnergyContextProvider(
        FailingTariffs(),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: NOW,
    )

    with pytest.raises(EnergyProviderError) as raised:
        composed.get_context(energy_context_for().horizon)

    diagnostic = raised.value.diagnostic
    assert diagnostic.code == "provider_invalid"
    assert diagnostic.provider_id == "tariff_live"
    assert "do-not-leak" not in str(diagnostic)


def test_negative_market_tariffs_are_valid() -> None:
    context = energy_context_for()
    points = [
        TariffPoint(slot=0, price_per_kwh=-0.001, currency="EUR"),
        *context.tariffs[1:],
    ]
    series = TariffSeries(
        horizon=context.horizon,
        source_id="omie_fixture",
        source_revision="quarter-hour-1",
        observed_at=NOW,
        points=points,
    )

    assert series.points[0].price_per_kwh == -0.001


def test_provider_models_are_strict_and_validate_slots() -> None:
    context = energy_context_for()
    with pytest.raises(ValidationError):
        TariffSeries(
            horizon=context.horizon,
            source_id="tariff_fixture",
            source_revision="one",
            observed_at=NOW,
            points=context.tariffs[:-1],
            unexpected="rejected",
        )

    with pytest.raises(ValidationError, match="ordered"):
        SolarForecastSeries(
            horizon=context.horizon,
            source_id="solar_fixture",
            source_revision="one",
            observed_at=NOW,
            points=[
                context.solar_forecast[1],
                context.solar_forecast[0],
                *context.solar_forecast[2:],
            ],
        )


def test_static_component_providers_are_read_only() -> None:
    tariffs, solar, battery = provider_inputs()

    assert not hasattr(StaticTariffProvider(tariffs), "execute")
    assert not hasattr(StaticSolarForecastProvider(solar), "execute")
    assert not hasattr(StaticBatteryProvider(battery), "execute")
