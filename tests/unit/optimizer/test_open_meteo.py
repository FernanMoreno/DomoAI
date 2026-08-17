from datetime import UTC, date, datetime

import pytest

from domoai.optimizer.open_meteo import (
    OpenMeteoForecastFile,
    OpenMeteoSolarProvider,
    parse_open_meteo_solar_forecast,
)
from domoai.optimizer.providers import EnergyProviderError
from tests.fixtures.energy import omie_horizon, open_meteo_config, open_meteo_payload


class FixtureOpenMeteoClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.requested_horizon = None

    def fetch_forecast(self, horizon, config):
        del config
        self.requested_horizon = horizon
        return OpenMeteoForecastFile(
            payload=self.payload,
            source_revision="forecast:Europe/Madrid:2026-08-16",
            observed_at=datetime(2026, 8, 16, 14, tzinfo=UTC),
        )


def test_parser_converts_irradiance_to_kw_and_caps_inverter() -> None:
    horizon = omie_horizon()
    points = parse_open_meteo_solar_forecast(
        open_meteo_payload(horizon),
        horizon=horizon,
        config=open_meteo_config(inverter_ac_max_kw=3.0),
    )

    assert len(points) == 96
    assert points[0].power == pytest.approx(3.0)
    assert points[-1].slot == 95


@pytest.mark.parametrize(
    ("session_date", "expected_slots"),
    [(date(2026, 3, 29), 92), (date(2026, 10, 25), 100)],
)
def test_parser_maps_dst_days_by_utc_instants(session_date, expected_slots) -> None:
    horizon = omie_horizon(session_date=session_date)
    payload = open_meteo_payload(horizon, irradiance_wm2=0)

    points = parse_open_meteo_solar_forecast(
        payload,
        horizon=horizon,
        config=open_meteo_config(),
    )

    assert horizon.slots == expected_slots
    assert len(points) == expected_slots
    assert [point.slot for point in points] == list(range(expected_slots))


def test_provider_returns_canonical_series_and_provenance() -> None:
    horizon = omie_horizon()
    client = FixtureOpenMeteoClient(open_meteo_payload(horizon))
    series = OpenMeteoSolarProvider(client, open_meteo_config()).get_forecast(horizon)

    assert client.requested_horizon == horizon
    assert series.source_id == "open_meteo_solar"
    assert series.source_revision == "forecast:Europe/Madrid:2026-08-16"
    assert len(series.points) == horizon.slots


def test_provider_sanitizes_client_failure() -> None:
    class FailingClient:
        def fetch_forecast(self, _horizon, _config):
            raise TimeoutError("token=must-not-cross-boundary")

    with pytest.raises(EnergyProviderError) as raised:
        OpenMeteoSolarProvider(FailingClient(), open_meteo_config()).get_forecast(
            omie_horizon()
        )

    assert raised.value.diagnostic.code == "provider_unavailable"
    assert "token" not in str(raised.value.diagnostic)
