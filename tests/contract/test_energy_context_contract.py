import pytest
from pydantic import ValidationError

from domoai.optimizer.energy import EnergyContext
from domoai.optimizer.scenario import Horizon
from tests.fixtures.energy import energy_context_for


def test_energy_context_contract_round_trips_versioned_payload() -> None:
    context = energy_context_for()
    payload = context.model_dump(mode="json")
    parsed = EnergyContext.model_validate(payload)

    assert payload["schema_version"] == "v1"
    assert parsed.horizon.slots == len(payload["tariffs"]) == len(payload["solar_forecast"])
    assert payload["battery"]["capacity_kwh"] == 6


def test_energy_context_contract_rejects_out_of_order_series() -> None:
    context = energy_context_for()
    payload = context.model_dump(mode="python")
    payload["tariffs"][0]["slot"], payload["tariffs"][1]["slot"] = (
        payload["tariffs"][1]["slot"],
        payload["tariffs"][0]["slot"],
    )

    with pytest.raises(ValidationError, match="ordered"):
        EnergyContext.model_validate(payload)


def test_energy_context_contract_rejects_horizon_metadata_mismatch() -> None:
    context = energy_context_for()
    horizon = Horizon(
        start=context.horizon.start,
        end=context.horizon.end,
        resolution_minutes=30,
        timezone="Europe/Madrid",
    )

    with pytest.raises(ValidationError):
        EnergyContext.model_validate(context.model_copy(update={"horizon": horizon}).model_dump())
