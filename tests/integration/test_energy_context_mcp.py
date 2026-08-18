import pytest

from tests.contract.test_domotics_mcp_contract import build_context, structured
from tests.fixtures.energy import energy_context_for


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
        "battery",
        "source_revision",
        "observed_at",
    }
    assert "protocol" not in str(result).lower()
