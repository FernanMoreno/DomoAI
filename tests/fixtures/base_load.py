"""Independent declared demand fixtures; no production forecasting helpers."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from domoai.config.settings import Settings
from domoai.optimizer.energy import SolarForecastPoint, TariffPoint
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.providers import SolarForecastSeries, TariffSeries

NOW = datetime(2026, 9, 10, 10, tzinfo=UTC)


def horizon() -> Horizon:
    return Horizon(
        start=NOW, end=NOW + timedelta(hours=2), resolution_minutes=15, timezone="Europe/Madrid"
    )


def profile_payload(**updates: object) -> dict:
    return {
        "schema_version": "v1",
        "tenant_id": "tenant-a",
        "household_id": "home-a",
        "source_id": "base-reserve",
        "source_revision": "r1",
        "declared_at": "2026-09-09T08:00:00Z",
        "valid_from": "2026-09-09T08:00:00Z",
        "valid_until": "2026-09-12T08:00:00Z",
        "power": 1.0,
        "unit": "kW",
        "kind": "declared_reserve",
        "scope": "uncontrolled_demand",
        **updates,
    }


def write_profile(path: Path, **updates: object) -> Path:
    path.write_text(json.dumps(profile_payload(**updates)), encoding="utf-8")
    return path


def settings_for(path: Path, **updates: object) -> Settings:
    return Settings(
        database_path=path,
        mcp_tenant_id="tenant-a",
        mcp_household_id="home-a",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6,
        solar_tilt=30,
        solar_azimuth=0,
        solar_performance_ratio=0.82,
        **updates,
    )


def tariffs(requested: Horizon) -> TariffSeries:
    return TariffSeries(
        horizon=requested,
        source_id="tariff-fixture",
        source_revision="r1",
        observed_at=NOW,
        points=[
            TariffPoint(slot=i, price_per_kwh=0.2, currency="EUR") for i in range(requested.slots)
        ],
    )


def solar(requested: Horizon) -> SolarForecastSeries:
    return SolarForecastSeries(
        horizon=requested,
        source_id="solar-fixture",
        source_revision="r1",
        observed_at=NOW,
        points=[SolarForecastPoint(slot=i, power=0) for i in range(requested.slots)],
    )
