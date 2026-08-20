from datetime import UTC, datetime
from time import perf_counter

import pytest

from domoai.mcp.ortools_server import create_ortools_server
from domoai.optimizer.scenario import Constraint, Horizon, Load, OptimizationScenario
from tests.integration.test_ortools_mcp_fixtures import build_context, structured


@pytest.mark.asyncio
async def test_fifty_load_scenario_finishes_within_local_target() -> None:
    adapter, context = await build_context()
    server = create_ortools_server(context)
    device_id = next(
        device.id for device in context.registry.devices if device.type.value == "light"
    )
    scenario = OptimizationScenario(
        id="performance-energy-050",
        horizon=Horizon(
            start=datetime(2026, 8, 15, tzinfo=UTC),
            end=datetime(2026, 8, 15, 2, tzinfo=UTC),
            resolution_minutes=15,
            timezone="Europe/Madrid",
        ),
        loads=[
            Load(
                id=f"light-load-{index:02d}",
                device_id=device_id,
                capability="brightness",
                command="set_brightness",
                value=60,
                unit="%",
                power=100,
                power_unit="W",
            )
            for index in range(50)
        ],
        constraints=[Constraint(type="max_house_power", value=10_000, unit="W")],
        solver_time_limit_seconds=5,
    )

    started = perf_counter()
    result = structured(
        await server.call_tool("optimize_scenario", {"scenario": scenario.model_dump(mode="json")})
    )
    elapsed = perf_counter() - started

    assert result["status"] in {"optimal", "feasible"}
    assert len(result["plan"]["commands"]) == 50
    assert adapter.calls == []
    assert elapsed < 5
