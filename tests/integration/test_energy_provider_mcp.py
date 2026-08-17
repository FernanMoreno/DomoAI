from datetime import UTC, datetime

import pytest

from domoai.domain.solar import SolarInstallationProfile
from domoai.mcp.domotics_server import create_domotics_server
from domoai.optimizer.omie import OmieDayAheadFile, OmieTariffProvider
from domoai.optimizer.open_meteo import (
    OpenMeteoForecastFile,
    OpenMeteoSolarConfig,
    OpenMeteoSolarProvider,
)
from domoai.optimizer.providers import (
    BatteryState,
    ComposedEnergyContextProvider,
    EnergyProviderDiagnostic,
    EnergyProviderError,
    SolarForecastSeries,
    StaticBatteryProvider,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
)
from tests.contract.test_domotics_mcp_contract import build_context, structured
from tests.fixtures.energy import (
    energy_context_for,
    omie_file_payload,
    omie_horizon,
    open_meteo_payload,
)


def composed_provider() -> ComposedEnergyContextProvider:
    context = energy_context_for()
    observed_at = datetime(2026, 8, 15, 12, tzinfo=UTC)
    tariffs = TariffSeries(
        horizon=context.horizon,
        source_id="tariff_fixture",
        source_revision="mcp-1",
        observed_at=observed_at,
        points=context.tariffs,
    )
    solar = SolarForecastSeries(
        horizon=context.horizon,
        source_id="solar_fixture",
        source_revision="mcp-1",
        observed_at=observed_at,
        points=context.solar_forecast,
    )
    battery = BatteryState(
        horizon=context.horizon,
        source_id="battery_fixture",
        source_revision="mcp-1",
        observed_at=observed_at,
        battery=context.battery,
    )
    return ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: datetime(2026, 8, 15, 12, 5, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_mcp_consumes_composed_provider_without_surface_changes() -> None:
    context = await build_context()
    context.energy_context_provider = composed_provider()
    server = create_domotics_server(context)
    requested = energy_context_for().horizon

    result = structured(
        await server.call_tool(
            "get_energy_context",
            {"horizon": requested.model_dump(mode="json")},
        )
    )

    assert result["schema_version"] == "v1"
    assert result["context"]["source_revision"].startswith("tariff:tariff_fixture@")
    assert result["context"]["battery"] is not None


@pytest.mark.asyncio
async def test_mcp_accepts_omie_provider_inside_existing_composer() -> None:
    horizon = omie_horizon()
    fixture = energy_context_for(horizon)
    observed_at = datetime(2026, 8, 16, 14, tzinfo=UTC)

    class Client:
        def fetch_day(self, _session_date):
            return OmieDayAheadFile(
                body=omie_file_payload(),
                source_revision="marginalpdbc_20260816.1",
                observed_at=observed_at,
            )

    solar = SolarForecastSeries(
        horizon=horizon,
        source_id="solar_fixture",
        source_revision="mcp-omie-1",
        observed_at=observed_at,
        points=fixture.solar_forecast,
    )
    battery = BatteryState(
        horizon=horizon,
        source_id="battery_fixture",
        source_revision="mcp-omie-1",
        observed_at=observed_at,
        battery=fixture.battery,
    )
    composed = ComposedEnergyContextProvider(
        OmieTariffProvider(Client()),
        StaticSolarForecastProvider(solar),
        StaticBatteryProvider(battery),
        now=lambda: datetime(2026, 8, 16, 14, 5, tzinfo=UTC),
    )
    context = await build_context()
    context.energy_context_provider = composed
    server = create_domotics_server(context)

    result = structured(
        await server.call_tool(
            "get_energy_context",
            {"horizon": horizon.model_dump(mode="json")},
        )
    )

    assert result["context"]["source_revision"].startswith(
        "tariff:omie_spain@marginalpdbc_20260816.1"
    )
    assert result["context"]["tariffs"][0]["price_per_kwh"] == 0.1


@pytest.mark.asyncio
async def test_mcp_accepts_omie_and_open_meteo_inside_existing_composer() -> None:
    horizon = omie_horizon()
    observed_at = datetime(2026, 8, 16, 14, tzinfo=UTC)

    class OmieClient:
        def fetch_day(self, _session_date):
            return OmieDayAheadFile(
                body=omie_file_payload(),
                source_revision="marginalpdbc_20260816.1",
                observed_at=observed_at,
            )

    class OpenMeteoClient:
        def fetch_forecast(self, _horizon, _config):
            return OpenMeteoForecastFile(
                payload=open_meteo_payload(horizon),
                source_revision="forecast:Europe/Madrid:2026-08-16",
                observed_at=observed_at,
            )

    profile = SolarInstallationProfile(
        latitude=40.4168,
        longitude=-3.7038,
        installed_kwp=6,
        tilt=30,
        azimuth=0,
        performance_ratio=0.82,
        inverter_ac_max_kw=5,
        timezone="Europe/Madrid",
    )
    composed = ComposedEnergyContextProvider(
        OmieTariffProvider(OmieClient()),
        OpenMeteoSolarProvider(OpenMeteoClient(), OpenMeteoSolarConfig.from_profile(profile)),
        now=lambda: datetime(2026, 8, 16, 14, 5, tzinfo=UTC),
    )
    context = await build_context()
    context.energy_context_provider = composed

    result = structured(
        await create_domotics_server(context).call_tool(
            "get_energy_context",
            {"horizon": horizon.model_dump(mode="json")},
        )
    )

    assert result["context"]["source_revision"].startswith(
        "tariff:omie_spain@"
    )
    assert result["context"]["solar_forecast"][0]["power"] == pytest.approx(3.444)


@pytest.mark.asyncio
async def test_mcp_keeps_provider_errors_in_safe_error_envelopes() -> None:
    context = await build_context()

    class FailingProvider:
        provider_id = "tariff_live"

        def get_tariffs(self, _horizon: object) -> TariffSeries:
            raise EnergyProviderError(
                EnergyProviderDiagnostic(
                    code="provider_unavailable",
                    provider_id="tariff_live",
                    message="tariff provider unavailable",
                    retryable=True,
                )
            )

    current = energy_context_for()
    context.energy_context_provider = ComposedEnergyContextProvider(
        FailingProvider(),
        StaticSolarForecastProvider(
            SolarForecastSeries(
                horizon=current.horizon,
                source_id="solar_fixture",
                source_revision="mcp-1",
                observed_at=datetime(2026, 8, 15, 12, tzinfo=UTC),
                points=current.solar_forecast,
            )
        ),
        now=lambda: datetime(2026, 8, 15, 12, 5, tzinfo=UTC),
    )
    server = create_domotics_server(context)

    result = structured(
        await server.call_tool(
            "get_energy_context",
            {"horizon": current.horizon.model_dump(mode="json")},
        )
    )

    assert result["error"]["code"] == "provider_unavailable"
    assert "traceback" not in str(result).lower()
