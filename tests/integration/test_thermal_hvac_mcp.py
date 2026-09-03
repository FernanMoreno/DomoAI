from datetime import UTC, datetime

import httpx
import pytest
from anyio import create_memory_object_stream, create_task_group
from mcp import ClientSession

from domoai.mcp.unified_server import create_unified_server
from domoai.optimizer.energy import (
    ExteriorTemperaturePoint,
    SolarForecastPoint,
    TariffPoint,
    ThermalProfile,
)
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.providers import (
    ComposedEnergyContextProvider,
    ExteriorTemperatureSeries,
    SolarForecastSeries,
    StaticExteriorTemperatureProvider,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
)
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from tests.contract.test_unified_mcp_contract import build_context
from tests.fixtures.energy import energy_horizon

_OBSERVED_AT = datetime(2026, 8, 15, 11, tzinfo=UTC)
_HORIZON = energy_horizon(slots=2, resolution_minutes=15)


def _golden_energy_context_provider() -> ComposedEnergyContextProvider:
    # Same golden preheat parameters already proven in
    # test_energy_optimization.py::test_hvac_shifts_to_cheap_period_while_staying_comfortable
    # and test_cp_sat_thermal.py::test_hard_comfort_min_forces_heating_into_the_cheap_slot:
    # flat cheap/expensive tariff split, cold exterior both slots.
    tariffs = TariffSeries(
        horizon=_HORIZON,
        source_id="tariff_fixture",
        source_revision="thermal-mcp-1",
        observed_at=_OBSERVED_AT,
        points=[
            TariffPoint(slot=0, price_per_kwh=0.05, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.50, currency="EUR"),
        ],
    )
    exterior_temperature = ExteriorTemperatureSeries(
        horizon=_HORIZON,
        source_id="exterior_temperature_fixture",
        source_revision="thermal-mcp-1",
        observed_at=_OBSERVED_AT,
        points=[
            ExteriorTemperaturePoint(slot=0, temperature_c=5.0),
            ExteriorTemperaturePoint(slot=1, temperature_c=5.0),
        ],
    )
    # Zero solar keeps this scenario isolated to the tariff/thermal decision,
    # matching test_hvac_shifts_to_cheap_period_while_staying_comfortable's
    # own zeroed-solar isolation (a real, nonzero solar forecast at some
    # slots would make heating a zero-cost tie, an unrelated confound).
    solar = SolarForecastSeries(
        horizon=_HORIZON,
        source_id="solar_fixture",
        source_revision="thermal-mcp-1",
        observed_at=_OBSERVED_AT,
        points=[
            SolarForecastPoint(slot=0, power=0.0),
            SolarForecastPoint(slot=1, power=0.0),
        ],
    )
    return ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        exterior_temperature=StaticExteriorTemperatureProvider(exterior_temperature),
        now=lambda: _OBSERVED_AT,
    )


@pytest.mark.asyncio
async def test_thermal_hvac_scenario_end_to_end_via_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _forbid_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("test_thermal_hvac_mcp must never reach the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", _forbid_network)
    monkeypatch.setattr(httpx.Client, "send", _forbid_network)

    _, context = await build_context()
    context.domotics.energy_context_provider = _golden_energy_context_provider()

    server = create_unified_server(context)
    client_to_server_send, client_to_server_receive = create_memory_object_stream(0)
    server_to_client_send, server_to_client_receive = create_memory_object_stream(0)

    async def run_server() -> None:
        await server._mcp_server.run(
            client_to_server_receive,
            server_to_client_send,
            server._mcp_server.create_initialization_options(),
        )

    async with create_task_group() as task_group:
        task_group.start_soon(run_server)
        async with ClientSession(server_to_client_receive, client_to_server_send) as session:
            await session.initialize()

            context_result = await session.call_tool(
                "get_energy_context",
                {"horizon": _HORIZON.model_dump(mode="json")},
            )
            assert context_result.structuredContent is not None
            energy_context = context_result.structuredContent["context"]
            assert energy_context["exterior_temperature_forecast"] is not None
            # thermal is scenario-author-supplied config, not fetched through
            # any provider (research.md Decision 8/9) -- get_energy_context
            # never populates it, matching a real MCP-connected agent's flow.
            assert energy_context["thermal"] is None
            energy_context["thermal"] = ThermalProfile(
                capacitance_kwh_per_c=5.0,
                ua_kw_per_c=0.2,
                initial_temperature_c=19.0,
                comfort_min_c=19.0,
                comfort_max_c=50.0,
                max_heat_kw=3.0,
                max_cool_kw=3.0,
                heating_cop=3.0,
                cooling_cop=2.5,
            ).model_dump(mode="json")

            scenario = OptimizationScenario(
                id="thermal-mcp-golden-1",
                horizon=_HORIZON,
                energy_context=energy_context,
                constraints=[
                    Constraint(type="comfort_temp_min", value=19.0, unit="degC", hard=True),
                ],
                objectives=[
                    Objective(name="minimize_energy_cost", direction="minimize"),
                ],
            )

            proposal_result = await session.call_tool(
                "optimize_scenario",
                {"scenario": scenario.model_dump(mode="json")},
            )
            assert proposal_result.structuredContent is not None
            proposal = proposal_result.structuredContent
        task_group.cancel_scope.cancel()

    # No actuator is bound in this scenario (proving the recurrence/objective
    # reach through the real MCP path is the point here, not Plan/command
    # emission -- that's already proven separately by T012/T013's dedicated
    # unit tests), so NO_ACTION_REQUIRED is the expected status: no Plan is
    # ever produced without a bound actuator, mirroring the same pattern
    # already established in test_hard_comfort_min_forces_heating_into_the_cheap_slot.
    assert proposal["status"] in {
        OptimizationStatus.FEASIBLE_HIERARCHY.value,
        OptimizationStatus.OPTIMAL_HIERARCHY.value,
        OptimizationStatus.NO_ACTION_REQUIRED.value,
    }
    slots = proposal["constraint_summary"]["slots"]
    temperatures = [slot["indoor_temperature_c"] for slot in slots]
    assert all(value >= 19.0 - 0.01 for value in temperatures)
    hvac_powers = [slot["hvac_power_kw"] for slot in slots]
    # Cost-driven front-loading into the cheap slot, same golden result
    # already proven in test_hvac_shifts_to_cheap_period_while_staying_comfortable,
    # now proven reachable end-to-end through the real MCP tool path.
    assert hvac_powers[0] >= hvac_powers[1] - 1e-6
    assert hvac_powers[0] > 0.0
