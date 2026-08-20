import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from domoai.optimizer.providers import (
    BatteryState,
    EnergyProviderDiagnostic,
    SolarForecastSeries,
    TariffSeries,
)
from tests.fixtures.energy import energy_context_for

ROOT = Path(__file__).resolve().parents[2]


def test_provider_contract_models_round_trip_as_v1() -> None:
    context = energy_context_for()
    tariff = TariffSeries(
        horizon=context.horizon,
        source_id="tariff_fixture",
        source_revision="revision-1",
        observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
        points=context.tariffs,
    )
    solar = SolarForecastSeries(
        horizon=context.horizon,
        source_id="solar_fixture",
        source_revision="revision-1",
        observed_at=tariff.observed_at,
        points=context.solar_forecast,
    )
    battery = BatteryState(
        horizon=context.horizon,
        source_id="battery_fixture",
        source_revision="revision-1",
        observed_at=tariff.observed_at,
        battery=context.battery,
    )

    assert TariffSeries.model_validate(tariff.model_dump(mode="python")).schema_version == "v1"
    assert (
        SolarForecastSeries.model_validate(solar.model_dump(mode="python")).points == solar.points
    )
    assert BatteryState.model_validate(battery.model_dump(mode="python")).battery == battery.battery


def test_provider_diagnostic_is_safe_and_strict() -> None:
    diagnostic = EnergyProviderDiagnostic(
        code="stale_provider_data",
        provider_id="tariff_live",
        message="provider data is stale",
        retryable=False,
        details={"age_seconds": 901, "max_age_seconds": 900},
    )

    assert diagnostic.schema_version == "v1"
    with pytest.raises(ValidationError):
        EnergyProviderDiagnostic.model_validate(
            diagnostic.model_dump(mode="python") | {"secret": "forbidden"}
        )


def test_provider_schemas_are_published_and_versioned() -> None:
    for name in (
        "tariff-series",
        "solar-forecast-series",
        "battery-state",
        "energy-provider-diagnostic",
    ):
        schema = json.loads(
            (ROOT / "schemas" / "v1" / f"{name}.schema.json").read_text(encoding="utf-8")
        )
        assert schema["properties"]["schema_version"]["const"] == "v1"


def test_provider_metadata_rejects_naive_timestamp_and_unsafe_revision() -> None:
    context = energy_context_for()
    with pytest.raises(ValidationError, match="timezone-aware"):
        TariffSeries(
            horizon=context.horizon,
            source_id="tariff_fixture",
            source_revision="revision-1",
            observed_at=datetime(2026, 8, 15, 12),
            points=context.tariffs,
        )

    with pytest.raises(ValidationError):
        TariffSeries(
            horizon=context.horizon,
            source_id="tariff_fixture",
            source_revision="revision?with-secret",
            observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
            points=context.tariffs,
        )
