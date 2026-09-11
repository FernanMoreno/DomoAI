"""Actual configured runtime/MCP/CP-SAT demand accounting, with no physical dispatch."""

from datetime import timedelta
from pathlib import Path

import pytest

from domoai.application import runtime_factory
from domoai.config.settings import Settings
from domoai.mcp.configured import build_configured_server
from domoai.optimizer.energy import EnergyContext, StaticEnergyContextProvider
from domoai.optimizer.omie import OmieTariffProvider
from domoai.optimizer.open_meteo import OpenMeteoSolarProvider
from domoai.optimizer.scenario import Constraint, Objective, OptimizationScenario
from domoai.runtime.clock import FixedClock
from tests.contract.test_ortools_mcp_contract import structured
from tests.fixtures.base_load import NOW, horizon, settings_for, solar, tariffs, write_profile


@pytest.fixture
def deterministic_sources(monkeypatch: pytest.MonkeyPatch) -> FixedClock:
    clock = FixedClock(NOW)
    monkeypatch.setattr(runtime_factory, "SystemClock", lambda: clock)
    # OMIE itself accepts full days only. These two-hour fixtures replace its
    # port, not the factory, composer, base provider, MCP worker or CP-SAT.
    monkeypatch.setattr(
        OmieTariffProvider,
        "get_tariffs",
        lambda self, h: tariffs(h).model_copy(update={"observed_at": clock.now()}),
    )
    monkeypatch.setattr(
        OpenMeteoSolarProvider,
        "get_forecast",
        lambda self, h: solar(h).model_copy(update={"observed_at": clock.now()}),
    )
    return clock


async def context_from_mcp(server, requested=None) -> dict:
    envelope = structured(
        await server.call_tool(
            "get_energy_context", {"horizon": (requested or horizon()).model_dump(mode="json")}
        )
    )
    assert "context" in envelope, envelope
    return envelope["context"]


@pytest.mark.parametrize("power", [1.0, 0.0, None])
async def test_configured_base_reaches_public_passive_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deterministic_sources,
    power: float | None,
) -> None:
    settings = settings_for(tmp_path / "home.sqlite3")
    if power is not None:
        path = write_profile(tmp_path / "base.json", power=power)
        monkeypatch.setenv("DOMOAI_BASE_LOAD_PROFILE_PATH", str(path))
        # Exercise environment propagation as well as direct Settings construction.
        from_env = Settings.from_environment()
        if (selected := getattr(from_env, "base_load_profile_path", None)) is not None:
            settings = settings.model_copy(update={"base_load_profile_path": selected})
    runtime, server = await build_configured_server(settings)
    before = runtime.state_store.runtime_revision
    try:
        payload = await context_from_mcp(server)
        scenario = OptimizationScenario(
            id="base-public",
            horizon=horizon(),
            constraints=[Constraint(type="max_grid_import", value=power or 0, unit="kW")],
            energy_context=EnergyContext.model_validate(payload),
            objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
        )
        result = structured(
            await server.call_tool(
                "optimize_scenario",
                {
                    "scenario": scenario.model_dump(mode="json"),
                    "validate_proposal": False,
                },
            )
        )
        assert result["status"] == "no_action_required", result
        assert result["objective_values"]["energy_cost"] == pytest.approx((power or 0) * 0.4)
        slots = result["constraint_summary"]["slots"]
        assert sum(slot["grid_import_kw"] * 0.25 for slot in slots) == pytest.approx(
            (power or 0) * 2
        )
        summary = structured(await server.call_tool("summarize_solution", {"result": result}))
        evidence = summary["constraint_summary"]["forecast_assumptions"]["base_load"]
        assert evidence["household_limit_guaranteed"] is False
        assert result["constraint_summary"]["hard_satisfied"] is True
        if power is None:
            assert payload["base_load_forecast"] is None
            assert evidence["status"] == "unknown"
            assert evidence["all_zero"] is None
            assert summary["hard_constraints_satisfied"] is None
            assert "unknown" in summary["headline"].lower()
        else:
            assert evidence["kind"] == "declared_reserve"
            assert evidence["all_zero"] is (power == 0)
            assert evidence["coverage"] == "complete"
            assert payload["base_load_source"]["observed_at"] == "2026-09-09T08:00:00Z"
            assert evidence["source_revision"] == payload["base_load_source"]["source_revision"]
            assert "modeled" in summary["headline"].lower()
        # Accepted A10 passive comparison must retain this evidence and identity.
        variation = scenario.model_copy(update={"id": "base-variation"})
        comparison = structured(
            await server.call_tool(
                "compare_scenarios",
                {
                    "baseline": scenario.model_dump(mode="json"),
                    "variations": {"same": variation.model_dump(mode="json")},
                },
            )
        )
        assert (
            comparison["baseline"]["constraint_summary"]["forecast_assumptions"]["base_load"]
            == evidence
        )
        assert comparison["variations"]["same"]["summary"]["scenario_id"] == "base-variation"
        assert runtime.adapter.calls == []
        assert runtime.plans == {}
        assert runtime.state_store.runtime_revision == before
    finally:
        await runtime.close()
        await runtime.close()


async def test_profile_scope_restart_revision_and_external_override(
    tmp_path: Path,
    deterministic_sources,
) -> None:
    profile = write_profile(tmp_path / "base.json")
    settings = settings_for(tmp_path / "a.sqlite3", base_load_profile_path=profile)
    runtime, server = await build_configured_server(settings)
    try:
        original = await context_from_mcp(server)
    finally:
        await runtime.close()
    runtime, server = await build_configured_server(settings)
    try:
        restarted = await context_from_mcp(server)
        assert restarted["base_load_source"] == original["base_load_source"]
    finally:
        await runtime.close()
    write_profile(profile, power=2)  # Same r1 label, changed semantic content.
    runtime, server = await build_configured_server(settings)
    try:
        changed = await context_from_mcp(server)
        assert changed["source_revision"] != original["source_revision"]
        assert changed["base_load_forecast"][0]["power"] == 2
    finally:
        await runtime.close()
    for key, value in [("mcp_tenant_id", "tenant-b"), ("mcp_household_id", "home-b")]:
        other = settings.model_copy(
            update={key: value, "database_path": tmp_path / f"{key}.sqlite3"}
        )
        with pytest.raises(ValueError, match="scope"):
            await build_configured_server(other)
    # Whole-provider override has priority even over a nonexistent profile.
    external = StaticEnergyContextProvider(EnergyContext.model_validate(original))
    override = settings.model_copy(
        update={"base_load_profile_path": tmp_path / "missing-private.json"}
    )
    runtime, server = await build_configured_server(override, energy_context_provider=external)
    try:
        assert runtime.energy_context_provider is external
        assert runtime.energy_closers == ()
        assert (await context_from_mcp(server))["base_load_source"] == original["base_load_source"]
    finally:
        await runtime.close()


async def test_expired_reserve_fails_publicly_without_fallback(
    tmp_path: Path,
    deterministic_sources: FixedClock,
) -> None:
    settings = settings_for(
        tmp_path / "expired.sqlite3", base_load_profile_path=write_profile(tmp_path / "base.json")
    )
    runtime, server = await build_configured_server(settings)
    deterministic_sources.set(NOW + timedelta(days=3))
    try:
        result = structured(
            await server.call_tool(
                "get_energy_context", {"horizon": horizon().model_dump(mode="json")}
            )
        )
        assert "context" not in result
        assert result["error"]["code"] == "base_reserve_not_valid", result
        assert runtime.adapter.calls == []
    finally:
        await runtime.close()


async def test_configured_base_plus_ev_is_three_kw_not_five(
    tmp_path: Path,
    deterministic_sources,
) -> None:
    from domoai.optimizer.cp_sat import CpSatOptimizer
    from domoai.optimizer.scenario import Constraint, EVChargingLoad

    settings = settings_for(
        tmp_path / "ev.sqlite3", base_load_profile_path=write_profile(tmp_path / "base.json")
    )
    runtime, server = await build_configured_server(settings)
    try:
        context = EnergyContext.model_validate(await context_from_mcp(server))
        await runtime.discovery.refresh()
        device = next(d for d in runtime.registry.devices if d.type.value == "cover")
        # Analysis-only EV, like existing optimizer fixtures; no actuator claim.
        scenario = OptimizationScenario(
            id="residual-plus-ev",
            horizon=horizon(),
            energy_context=context,
            ev_loads=[
                EVChargingLoad(
                    id="ev",
                    device_id=device.id,
                    capability="position",
                    command="set_position",
                    capacity_kwh=10,
                    initial_soc_kwh=0,
                    target_soc_kwh=3.5,
                    max_charge_kw=2,
                    deadline_slot=7,
                    charge_efficiency=1,
                )
            ],
            constraints=[Constraint(type="max_grid_import", value=3, unit="kW")],
        )
        result = CpSatOptimizer(runtime.registry).optimize(scenario)
        assert "slots" in result.constraint_summary, result.model_dump(mode="json")
        slots = result.constraint_summary["slots"]
        assert [s["grid_import_kw"] for s in slots] == pytest.approx([3] * 7 + [1])
        assert [s["ev_charge_kw"] for s in slots] == pytest.approx([2] * 7 + [0])
        assert result.objective_values["energy_cost"] == pytest.approx(1.1)
        assert runtime.adapter.calls == []
    finally:
        await runtime.close()


async def test_full_day_builtin_sources_and_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    from domoai.optimizer.omie import OmieDayAheadFile, OmieTariffHttpClient
    from domoai.optimizer.open_meteo import OpenMeteoForecastFile, OpenMeteoHttpClient
    from tests.fixtures.energy import omie_file_payload, omie_horizon, open_meteo_payload

    monkeypatch.setattr(runtime_factory, "SystemClock", lambda: FixedClock(NOW))
    requested = omie_horizon(session_date=date(2026, 9, 10))
    assert requested.slots == 96  # Explicit non-DST day: 24 actual hours.
    monkeypatch.setattr(
        OmieTariffHttpClient,
        "fetch_day",
        lambda self, day: OmieDayAheadFile(
            omie_file_payload(session_date=day, spain_price=200), "day-1", NOW
        ),
    )
    monkeypatch.setattr(
        OpenMeteoHttpClient,
        "fetch_forecast",
        lambda self, h, config: OpenMeteoForecastFile(
            open_meteo_payload(h, irradiance_wm2=0), "day-1", NOW
        ),
    )
    closed = []
    for cls in (OmieTariffHttpClient, OpenMeteoHttpClient):
        original = cls.close

        def close(client, original=original, name=cls.__name__):
            closed.append(name)
            original(client)

        monkeypatch.setattr(cls, "close", close)
    settings = settings_for(
        tmp_path / "full-day.sqlite3", base_load_profile_path=write_profile(tmp_path / "base.json")
    )
    runtime, server = await build_configured_server(settings)
    try:
        context = EnergyContext.model_validate(await context_from_mcp(server, requested))
        scenario = OptimizationScenario(
            id="full-day",
            horizon=requested,
            energy_context=context,
            objectives=[Objective(name="minimize_energy_cost", direction="minimize")],
        )
        result = structured(
            await server.call_tool(
                "optimize_scenario",
                {
                    "scenario": scenario.model_dump(mode="json"),
                    "validate_proposal": False,
                },
            )
        )
        assert result["status"] == "no_action_required", result
        assert result["objective_values"]["energy_cost"] == pytest.approx(4.8)
        assert sum(s["grid_import_kw"] * 0.25 for s in result["constraint_summary"]["slots"]) == 24
        assert "base_load:base-reserve@" in context.source_revision
        assert runtime.adapter.calls == []
    finally:
        await runtime.close()
        await runtime.close()
    assert sorted(closed) == ["OmieTariffHttpClient", "OpenMeteoHttpClient"]


async def test_two_active_households_do_not_share_profile_or_revision(
    tmp_path: Path,
    deterministic_sources,
) -> None:
    settings_a = settings_for(
        tmp_path / "a.sqlite3", base_load_profile_path=write_profile(tmp_path / "a.json", power=1)
    )
    settings_b = settings_a.model_copy(
        update={
            "database_path": tmp_path / "b.sqlite3",
            "mcp_household_id": "home-b",
            "base_load_profile_path": write_profile(
                tmp_path / "b.json", household_id="home-b", power=2
            ),
        }
    )
    runtime_a, server_a = await build_configured_server(settings_a)
    try:
        runtime_b, server_b = await build_configured_server(settings_b)
        try:
            first = await context_from_mcp(server_a)
            other = await context_from_mcp(server_b)
            again = await context_from_mcp(server_a)
            assert first == again
            assert first["base_load_forecast"][0]["power"] == 1
            assert other["base_load_forecast"][0]["power"] == 2
            assert first["source_revision"] != other["source_revision"]
            assert runtime_a.adapter.calls == runtime_b.adapter.calls == []
        finally:
            await runtime_b.close()
    finally:
        await runtime_a.close()


def test_invalid_profile_is_rejected_before_http_resources_are_acquired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_client(*args, **kwargs):
        raise AssertionError("invalid profile acquired HTTP resources")

    monkeypatch.setattr(runtime_factory, "OmieTariffHttpClient", unexpected_client)
    monkeypatch.setattr(runtime_factory, "OpenMeteoHttpClient", unexpected_client)
    settings = settings_for(
        tmp_path / "invalid.sqlite3",
        base_load_profile_path=write_profile(tmp_path / "private.json", scope="net_meter"),
    )
    with pytest.raises(ValueError, match="base reserve profile is invalid"):
        runtime_factory._create_energy_context_provider(settings, clock=FixedClock(NOW))


@pytest.mark.parametrize("case", ["mutated_unit", "copied_infinity", "copied_source"])
async def test_configured_mcp_rejects_invalid_typed_base_provider_result(tmp_path, case) -> None:
    from tests.unit.optimizer.test_base_load import BaseProvider, composer, invalid_returned_series

    runtime, server = await build_configured_server(
        settings_for(tmp_path / "invalid-base.sqlite3"),
        energy_context_provider=composer(BaseProvider(invalid_returned_series(case))),
    )
    try:
        envelope = structured(
            await server.call_tool(
                "get_energy_context", {"horizon": horizon().model_dump(mode="json")}
            )
        )
        assert "context" not in envelope
        assert envelope["error"]["code"] == "provider_invalid"
        assert "private-token" not in str(envelope)
        assert runtime.adapter.calls == []
    finally:
        await runtime.close()
