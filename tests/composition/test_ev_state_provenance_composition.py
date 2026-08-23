from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from domoai.optimizer.energy import EVState, StaticEnergyContextProvider
from domoai.optimizer.scenario import ComfortLoad, EVChargingLoad, Horizon, OptimizationScenario
from domoai.skills.workflow import EnergySkillRequest, EnergySkillWorkflow
from tests.fixtures.energy import energy_context_for
from tests.fixtures.skill_workflow import build_workflow_fixture


def _structured(result: object) -> dict[str, Any]:
    if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict):
        return cast(dict[str, Any], result[1])
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def _ev_load(device_id: str) -> EVChargingLoad:
    return EVChargingLoad(
        id="ev-composition-1",
        device_id=device_id,
        capability="position",
        command="set_position",
        capacity_kwh=10.0,
        initial_soc_kwh=2.0,
        target_soc_kwh=4.0,
        max_charge_kw=4.0,
        deadline_slot=3,
        charge_efficiency=0.95,
    )


@pytest.mark.composition
@pytest.mark.asyncio
async def test_required_devices_union_includes_ev_and_comfort_loads() -> None:
    fixture = await build_workflow_fixture()
    device_ids = [device.id for device in fixture.domotics_context.registry.devices]
    ev_device, comfort_device = device_ids[2], device_ids[3]
    request = EnergySkillRequest(
        scenario=OptimizationScenario(
            id="required-devices-composition-1",
            horizon=Horizon(
                start=datetime(2026, 8, 15, tzinfo=UTC),
                end=datetime(2026, 8, 15, 1, tzinfo=UTC),
                resolution_minutes=15,
                timezone="Europe/Madrid",
            ),
            ev_loads=[_ev_load(ev_device)],
            comfort_loads=[
                ComfortLoad(
                    id="comfort-composition-1",
                    device_id=comfort_device,
                    capability="target_temperature",
                    command="set_temperature",
                    value=21,
                    power=0.5,
                    power_unit="kW",
                    earliest_slot=0,
                    deadline_slot=2,
                    min_active_slots=1,
                    end_command="set_temperature",
                    end_value=18,
                )
            ],
        )
    )

    required = EnergySkillWorkflow._required_devices(request)

    assert ev_device in required
    assert comfort_device in required


@pytest.mark.composition
@pytest.mark.asyncio
async def test_executable_ev_proposal_requires_provider_state_and_matching_assumptions() -> None:
    fixture = await build_workflow_fixture()
    ev_device = next(
        device.id
        for device in fixture.domotics_context.registry.devices
        if device.type.value == "cover"
    )
    scenario = OptimizationScenario(
        id="ev-provenance-composition-1",
        horizon=energy_context_for(with_battery=False).horizon,
        energy_context=energy_context_for(with_battery=False),
        ev_loads=[_ev_load(ev_device)],
    )

    missing = _structured(
        await fixture.router.call(
            "mcp",
            "optimize_scenario",
            {"scenario": scenario.model_dump(mode="json"), "validate_proposal": True},
        )
    )
    assert missing["status"] == "invalid"
    assert any(
        diagnostic["code"] == "ev_state_provenance_missing" for diagnostic in missing["diagnostics"]
    )

    observed_at = datetime(2026, 8, 15, tzinfo=UTC)
    state = EVState(
        device_id=ev_device,
        connected=True,
        soc_kwh=2.0,
        capacity_kwh=10.0,
        max_charge_kw=4.0,
        observed_at=observed_at,
        received_at=observed_at,
        source_revision=scenario.energy_context.source_revision,
        source_ref=fixture.domotics_context.discovery.state_store.peek(
            ev_device, "position"
        ).source_ref,
    )
    context = scenario.energy_context.model_copy(update={"ev_states": [state]})
    valid = scenario.model_copy(update={"energy_context": context})
    fixture.domotics_context.energy_context_provider = StaticEnergyContextProvider(context)
    result = _structured(
        await fixture.router.call(
            "mcp",
            "optimize_scenario",
            {"scenario": valid.model_dump(mode="json"), "validate_proposal": True},
        )
    )

    assert result["status"] in {"optimal", "feasible", "optimal_hierarchy", "feasible_hierarchy"}
