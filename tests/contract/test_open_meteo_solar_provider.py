from datetime import UTC, datetime

import httpx
import pytest

from domoai.optimizer.open_meteo import (
    OpenMeteoForecastFile,
    OpenMeteoHttpClient,
    OpenMeteoSolarConfig,
    OpenMeteoSolarProvider,
    parse_open_meteo_solar_forecast,
)
from domoai.optimizer.providers import EnergyProviderError
from tests.fixtures.energy import omie_horizon, open_meteo_config, open_meteo_payload


def test_http_client_uses_public_15_minute_contract() -> None:
    requests: list[httpx.Request] = []
    horizon = omie_horizon()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=open_meteo_payload(horizon),
        )

    client = OpenMeteoHttpClient(transport=httpx.MockTransport(handler))
    try:
        downloaded = client.fetch_forecast(horizon, open_meteo_config())
    finally:
        client.close()

    assert str(requests[0].url).startswith("https://api.open-meteo.com/v1/forecast?")
    assert requests[0].url.params["minutely_15"] == "global_tilted_irradiance"
    assert requests[0].url.params["timeformat"] == "unixtime"
    assert requests[0].url.params["timezone"] == "Europe/Madrid"
    assert downloaded.source_revision.startswith("forecast:Europe/Madrid:")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["minutely_15"].update(
            {"global_tilted_irradiance": payload["minutely_15"]["global_tilted_irradiance"][:-1]}
        ),
        lambda payload: payload["minutely_15"].update(
            {"time": payload["minutely_15"]["time"][:1] * 2 + payload["minutely_15"]["time"][2:]}
        ),
        lambda payload: payload["minutely_15"].update(
            {
                "global_tilted_irradiance": [float("nan")]
                + payload["minutely_15"]["global_tilted_irradiance"][1:]
            }
        ),
    ],
)
def test_parser_rejects_incomplete_duplicate_or_non_finite_payload(mutate) -> None:
    horizon = omie_horizon()
    payload = open_meteo_payload(horizon)
    mutate(payload)

    with pytest.raises(ValueError):
        parse_open_meteo_solar_forecast(
            payload,
            horizon=horizon,
            config=open_meteo_config(),
        )


def test_provider_rejects_timezone_mismatch_before_client_call() -> None:
    called = False

    class Client:
        def fetch_forecast(self, _horizon, _config):
            nonlocal called
            called = True
            raise AssertionError("network must not be called")

    with pytest.raises(EnergyProviderError) as raised:
        OpenMeteoSolarProvider(Client(), open_meteo_config(timezone="UTC")).get_forecast(
            omie_horizon()
        )

    assert raised.value.diagnostic.code == "unsupported_horizon"
    assert called is False


def test_provider_rejects_unsafe_revision() -> None:
    horizon = omie_horizon()

    class Client:
        def fetch_forecast(self, _horizon, _config):
            return OpenMeteoForecastFile(
                payload=open_meteo_payload(horizon),
                source_revision="forecast?token=secret",
                observed_at=datetime(2026, 8, 16, 14, tzinfo=UTC),
            )

    with pytest.raises(EnergyProviderError) as raised:
        OpenMeteoSolarProvider(Client(), open_meteo_config()).get_forecast(horizon)

    assert raised.value.diagnostic.code == "provider_invalid"
    assert "secret" not in str(raised.value.diagnostic)


def test_provider_sanitizes_malformed_json() -> None:
    horizon = omie_horizon()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    client = OpenMeteoHttpClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(EnergyProviderError) as raised:
            OpenMeteoSolarProvider(client, open_meteo_config()).get_forecast(horizon)
    finally:
        client.close()

    assert raised.value.diagnostic.code == "provider_invalid"
    assert "not-json" not in str(raised.value.diagnostic)


def test_config_rejects_invalid_installation_assumptions() -> None:
    with pytest.raises(ValueError):
        OpenMeteoSolarConfig(
            latitude=40,
            longitude=-3,
            installed_kwp=0,
            tilt=30,
            azimuth=0,
            performance_ratio=0.8,
            timezone="Europe/Madrid",
        )
