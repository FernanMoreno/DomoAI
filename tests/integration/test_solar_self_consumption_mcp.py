from datetime import UTC, datetime

import httpx
import pytest
from anyio import create_memory_object_stream, create_task_group
from mcp import ClientSession

from domoai.mcp.unified_server import create_unified_server
from domoai.optimizer.energy import SolarForecastPoint, TariffPoint
from domoai.optimizer.ports import OptimizationStatus
from domoai.optimizer.providers import (
    ComposedEnergyContextProvider,
    SolarForecastSeries,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
)
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from tests.contract.test_unified_mcp_contract import build_context
from tests.fixtures.energy import energy_horizon, flexible_load

_OBSERVED_AT = datetime(2026, 8, 15, 11, tzinfo=UTC)
_HORIZON = energy_horizon(slots=2, resolution_minutes=15)


def _golden_energy_context_provider() -> ComposedEnergyContextProvider:
    # Same golden data as test_energy_optimization.py's
    # test_maximize_solar_self_consumption_shifts_load_into_solar_slot: flat
    # tariff removes any price confound, solar 0.0 kW then 5.0 kW leaves
    # exactly one mathematically optimal slot for the flexible load.
    tariffs = TariffSeries(
        horizon=_HORIZON,
        source_id="tariff_fixture",
        source_revision="solar-mcp-1",
        observed_at=_OBSERVED_AT,
        points=[
            TariffPoint(slot=0, price_per_kwh=0.10, currency="EUR"),
            TariffPoint(slot=1, price_per_kwh=0.10, currency="EUR"),
        ],
    )
    solar = SolarForecastSeries(
        horizon=_HORIZON,
        source_id="solar_fixture",
        source_revision="solar-mcp-1",
        observed_at=_OBSERVED_AT,
        points=[
            SolarForecastPoint(slot=0, power=0.0),
            SolarForecastPoint(slot=1, power=5.0),
        ],
    )
    return ComposedEnergyContextProvider(
        StaticTariffProvider(tariffs),
        StaticSolarForecastProvider(solar),
        now=lambda: _OBSERVED_AT,
    )


@pytest.mark.asyncio
async def test_maximize_solar_self_consumption_end_to_end_via_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _forbid_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "test_solar_self_consumption_mcp must never reach the network"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", _forbid_network)
    monkeypatch.setattr(httpx.Client, "send", _forbid_network)

    _, context = await build_context()
    context.domotics.energy_context_provider = _golden_energy_context_provider()
    device_id = next(
        device.id
        for device in context.domotics.registry.devices
        if device.type.value == "switch"
    )

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

            scenario = OptimizationScenario(
                id="solar-mcp-golden-1",
                horizon=_HORIZON,
                energy_context=energy_context,
                loads=[
                    flexible_load(
                        device_id,
                        earliest_slot=0,
                        latest_slot=1,
                        duration_slots=1,
                        power_kw=1.0,
                    )
                ],
                constraints=[
                    Constraint(type="max_grid_import", value=10, unit="kW"),
                    Constraint(type="max_grid_export", value=10, unit="kW"),
                ],
                objectives=[
                    Objective(name="maximize_solar_self_consumption", direction="maximize")
                ],
            )

            proposal_result = await session.call_tool(
                "optimize_scenario",
                {"scenario": scenario.model_dump(mode="json")},
            )
            assert proposal_result.structuredContent is not None
            proposal = proposal_result.structuredContent
        task_group.cancel_scope.cancel()

    assert proposal["status"] in {
        OptimizationStatus.FEASIBLE_HIERARCHY.value,
        OptimizationStatus.OPTIMAL_HIERARCHY.value,
    }
    assert proposal["plan"] is not None
    command = next(
        cmd for cmd in proposal["plan"]["commands"] if cmd["device_id"] == device_id
    )
    assert command["intent"] == "scheduled_slot:1"
