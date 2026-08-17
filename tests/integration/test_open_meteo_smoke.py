"""Opt-in read-only smoke against the public Open-Meteo forecast service."""

import os
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from domoai.config.solar_profile import JsonSolarInstallationProfileSource
from domoai.optimizer.horizon import Horizon
from domoai.optimizer.open_meteo import (
    OpenMeteoHttpClient,
    OpenMeteoSolarConfig,
    OpenMeteoSolarProvider,
)


def test_live_open_meteo_smoke_is_opt_in() -> None:
    if os.getenv("DOMOAI_OPEN_METEO_LIVE") != "1":
        pytest.skip("Set DOMOAI_OPEN_METEO_LIVE=1 to run the public Open-Meteo smoke")

    profile_path = os.getenv("DOMOAI_SOLAR_PROFILE_PATH")
    if profile_path:
        config = OpenMeteoSolarConfig.from_profile(
            JsonSolarInstallationProfileSource(Path(profile_path)).load()
        )
    else:
        timezone_name = os.getenv("DOMOAI_SOLAR_TIMEZONE", "Europe/Madrid")
        config = OpenMeteoSolarConfig(
            latitude=float(os.environ["DOMOAI_SOLAR_LAT"]),
            longitude=float(os.environ["DOMOAI_SOLAR_LON"]),
            installed_kwp=float(os.environ["DOMOAI_SOLAR_KWP"]),
            tilt=float(os.environ["DOMOAI_SOLAR_TILT"]),
            azimuth=float(os.environ["DOMOAI_SOLAR_AZIMUTH"]),
            performance_ratio=float(os.environ["DOMOAI_SOLAR_PERFORMANCE_RATIO"]),
            inverter_ac_max_kw=(
                float(os.environ["DOMOAI_SOLAR_INVERTER_AC_MAX_KW"])
                if "DOMOAI_SOLAR_INVERTER_AC_MAX_KW" in os.environ
                else None
            ),
            timezone=timezone_name,
        )
    timezone = ZoneInfo(config.timezone)
    session_date = datetime.now(timezone).date()
    start = datetime.combine(session_date, time.min, tzinfo=timezone)
    horizon = Horizon(
        start=start,
        end=datetime.combine(session_date + timedelta(days=1), time.min, tzinfo=timezone),
        resolution_minutes=15,
        timezone=config.timezone,
    )
    client = OpenMeteoHttpClient()
    try:
        series = OpenMeteoSolarProvider(client, config).get_forecast(horizon)
    finally:
        client.close()

    assert len(series.points) == horizon.slots
    assert all(point.power >= 0 for point in series.points)
    assert series.source_id == "open_meteo_solar"
