from pathlib import Path

import pytest

from domoai.config.settings import Settings
from domoai.mcp.configured import build_configured_server
from tests.contract.test_domotics_mcp_contract import build_context, structured
from tests.fixtures.energy import energy_context_for


class _MinimalFakeEnergyContextProvider:
    """Smallest possible stand-in satisfying the EnergyContextProvider Protocol."""

    def get_context(self, horizon: object) -> object:
        raise NotImplementedError("not invoked by this test")


@pytest.mark.asyncio
async def test_energy_context_provider_is_available_through_semantic_mcp_read() -> None:
    from domoai.mcp.domotics_server import create_domotics_server

    context = await build_context()
    result = structured(
        await create_domotics_server(context).call_tool(
            "get_energy_context",
            {"horizon": energy_context_for().horizon.model_dump(mode="json")},
        )
    )

    assert result["context"]["source_revision"] == "fixture-energy-1"
    assert set(result["context"]) == {
        "schema_version",
        "horizon",
        "tariffs",
        "solar_forecast",
        "base_load_forecast",
        "export_tariffs",
        "battery",
        "ev_states",
        "thermal",
        "exterior_temperature_forecast",
        "source_revision",
        "observed_at",
    }
    assert "protocol" not in str(result).lower()


async def test_configured_server_get_energy_context_tool_serves_custom_provider_data(
    tmp_path: Path,
) -> None:
    # Spec 161 SC-001 / quickstart Scenario 5: an MCP-connected agent talking
    # to the real production entrypoint (build_configured_server) must be
    # able to read data that actually came from a supplied custom provider,
    # not just observe object identity on RuntimeComposition.
    from domoai.optimizer.energy import StaticEnergyContextProvider

    horizon = energy_context_for().horizon
    custom_provider = StaticEnergyContextProvider(
        energy_context_for(horizon, source_revision="my_custom_source")
    )
    settings = Settings(
        database_path=tmp_path / "configured-tool-call-custom-provider.sqlite3",
        energy_live=True,
    )

    runtime, server = await build_configured_server(
        settings,
        energy_context_provider=custom_provider,
    )
    try:
        result = structured(
            await server.call_tool(
                "get_energy_context",
                {"horizon": horizon.model_dump(mode="json")},
            )
        )
        assert result["context"]["source_revision"] == "my_custom_source"
    finally:
        await runtime.close()


async def test_build_configured_server_forwards_ev_charging_bindings(tmp_path: Path) -> None:
    # Spec 162 User Story 1 / SC-001: build_configured_server (the real
    # stdio/HTTP MCP entrypoint) must forward ev_charging_bindings to
    # build_runtime. Structural assertion, not a get_energy_context call: the
    # default (no energy_context_provider override) composer here wraps the
    # REAL OMIE/Open-Meteo HTTP clients (no network mocking in this test,
    # same reasoning as test_runtime_factory_wires_ev_charging_bindings_into_default_composer).
    # get_energy_context's generic tool-call mechanism (context.
    # energy_context_provider.get_context(...), indifferent to what composed
    # it) is already proven end-to-end by
    # test_configured_server_get_energy_context_tool_serves_custom_provider_data
    # (Spec 161) -- the only incremental fact this test needs to prove is
    # that the new parameter actually reaches the composer that tool reads.
    from domoai.domain.energy import EVActuator, EVChargingBinding
    from domoai.optimizer.providers import ComposedEnergyContextProvider

    binding = EVChargingBinding.model_validate(
        {
            "provider_id": "fixture",
            "device_id": "ev.home",
            "actuator": EVActuator(
                device_id="ev.home",
                capability="ev_charging",
                charge_command="charge_ev",
                stop_command="stop_ev",
                connected_capability="ev.connected",
                departure_capability="ev.departure_at",
                max_charge_kw=7.4,
            ),
            "soc_capability": "ev.soc",
            "capacity_capability": "ev.capacity",
        }
    )
    settings = Settings(
        database_path=tmp_path / "configured-ev-charging-bindings.sqlite3",
        energy_live=True,
        tariff_provider="omie",
        solar_provider="open_meteo",
        solar_latitude=40.4168,
        solar_longitude=-3.7038,
        solar_installed_kwp=6,
        solar_tilt=30,
        solar_azimuth=0,
        solar_performance_ratio=0.82,
    )

    runtime, _server = await build_configured_server(settings, ev_charging_bindings=(binding,))
    try:
        assert isinstance(runtime.energy_context_provider, ComposedEnergyContextProvider)
        ev_providers = runtime.energy_context_provider.ev_providers
        assert len(ev_providers) == 1
        assert ev_providers[0].provider_id == "fixture"
    finally:
        await runtime.close()


async def test_build_configured_server_forwards_external_energy_context_provider(
    tmp_path: Path,
) -> None:
    # Spec 161: build_configured_server (the real stdio/HTTP MCP entrypoint)
    # must forward energy_context_provider to build_runtime so an
    # MCP-connected host can actually reach a supplied provider, not just
    # direct build_runtime callers.
    custom_provider = _MinimalFakeEnergyContextProvider()
    settings = Settings(
        database_path=tmp_path / "configured-external-energy-provider.sqlite3",
        energy_live=True,
    )

    runtime, _server = await build_configured_server(
        settings,
        energy_context_provider=custom_provider,
    )
    try:
        assert runtime.energy_context_provider is custom_provider
    finally:
        await runtime.close()
