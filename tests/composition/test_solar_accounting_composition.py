"""Real composer/worker/CP-SAT/public summaries, with isolated passive households."""

from pathlib import Path

import httpx
import pytest

from domoai.mcp.configured import build_configured_server
from domoai.optimizer.energy import BaseLoadPoint, EnergyContext
from domoai.optimizer.providers import (
    BaseLoadForecastSeries,
    BatteryState,
    ComposedEnergyContextProvider,
    SolarForecastSeries,
    StaticBatteryProvider,
    StaticSolarForecastProvider,
    StaticTariffProvider,
    TariffSeries,
)
from domoai.optimizer.scenario import OptimizationScenario
from tests.contract.test_ortools_mcp_contract import structured
from tests.fixtures.base_load import settings_for
from tests.integration.test_energy_optimization import solar_accounting_scenario


class _BaseProvider:
    provider_id = "solar-base-fixture"

    def __init__(self, series):
        self.series = series

    def get_forecast(self, horizon):
        assert horizon == self.series.horizon
        return self.series


@pytest.mark.parametrize("household,base", [("solar-home-a", 1), ("solar-home-b", .5)])
async def test_composed_solar_accounting_remains_passive_and_keeps_base_assumptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, household, base,
) -> None:
    def forbid_network(*args, **kwargs):
        raise AssertionError("Solar accounting fixture attempted external network access")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbid_network)
    monkeypatch.setattr(httpx.Client, "send", forbid_network)
    scenario = solar_accounting_scenario(base=(base,))
    original = scenario.energy_context
    assert original is not None
    h = scenario.horizon
    metadata = {"horizon": h, "source_revision": "accounting-r1", "observed_at": h.start}
    composer = ComposedEnergyContextProvider(
        StaticTariffProvider(TariffSeries(
            **metadata, source_id="solar-tariff-fixture", points=original.tariffs,
        )),
        StaticSolarForecastProvider(SolarForecastSeries(
            **metadata, source_id="solar-fixture", points=original.solar_forecast,
        )),
        StaticBatteryProvider(BatteryState(
            **metadata, source_id="battery-fixture", battery=original.battery,
        )),
        export_tariffs=StaticTariffProvider(TariffSeries(
            **metadata, source_id="export-fixture", points=original.export_tariffs,
        )),
        base_load=_BaseProvider(BaseLoadForecastSeries(
            **metadata, source_id="solar-base-fixture", kind="fixture",
            scope="uncontrolled_demand", points=[BaseLoadPoint(slot=0, power=base)],
        )),
        now=lambda: h.start,
    )
    settings = settings_for(tmp_path / "home.sqlite3").model_copy(
        update={"mcp_household_id": household}
    )
    runtime, server = await build_configured_server(settings, energy_context_provider=composer)
    before_revision = runtime.state_store.runtime_revision
    before_events = tuple(runtime.audit.events)
    before_states = [s.model_dump() for s in await runtime.state_store.all()]
    try:
        context_payload = structured(await server.call_tool("get_energy_context", {
            "horizon": h.model_dump(mode="json"),
        }))
        context = EnergyContext.model_validate(context_payload["context"])
        scenario = OptimizationScenario(
            id=scenario.id, horizon=h, energy_context=context, objectives=scenario.objectives,
        )
        assert scenario.schema_version == context.schema_version == "v2"
        first_accounting = None
        for _ in range(2):
            result = structured(await server.call_tool("optimize_scenario", {
                "scenario": scenario.model_dump(mode="json"), "validate_proposal": False,
            }))
            assert result.get("status") == "no_action_required", result
            assert result["plan"] is None
            assert result["objective_values"]["solar_self_consumption_kwh"] == pytest.approx(base)
            evidence = result["constraint_summary"]["solar_accounting"]
            if first_accounting is not None:
                assert evidence == first_accounting
            first_accounting = evidence
            summary = structured(await server.call_tool("summarize_solution", {"result": result}))
            explanation = structured(await server.call_tool("explain_solution", {"result": result}))
            for public in (summary, explanation):
                assert public["constraint_summary"]["solar_accounting"] == evidence
                assumptions = public["constraint_summary"]["forecast_assumptions"]
                assert assumptions["base_load"]["kind"] == "fixture"
                assert assumptions["base_load"]["household_limit_guaranteed"] is False
                assert assumptions["solar_accounting"]["evidence_kind"] == "modeled_attribution"
            # This separate hypothetical input has no supplied base forecast.
            # The baseline's v2 context and provenance remain intact.
            unknown_context = EnergyContext(
                horizon=h, tariffs=context.tariffs, solar_forecast=context.solar_forecast,
                battery=context.battery, export_tariffs=context.export_tariffs,
                source_revision="unknown-base-fixture", observed_at=context.observed_at,
            )
            variation = OptimizationScenario(
                id="solar-unknown-base", horizon=h, energy_context=unknown_context,
                objectives=scenario.objectives,
            )
            comparison = structured(await server.call_tool("compare_scenarios", {
                "baseline": scenario.model_dump(mode="json"),
                "variations": {"unknown": variation.model_dump(mode="json")},
            }))
            alternative = comparison["variations"]["unknown"]
            assert alternative["diff"]["solar_self_consumption_kwh"] == pytest.approx(-base)
            assert alternative["summary"]["hard_constraints_satisfied"] is None
            assert alternative["summary"]["constraint_summary"]["solar_accounting"][
                "self_consumption_kwh"
            ] == 0
        assert runtime.adapter.calls == []
        assert runtime.plans == {}
        assert runtime.state_store.runtime_revision == before_revision
        assert runtime.audit.events == before_events
        assert [s.model_dump() for s in await runtime.state_store.all()] == before_states
    finally:
        await runtime.close()
