import pytest
from pydantic import ValidationError

from domoai.optimizer.energy import EnergyContext, StaticEnergyContextProvider
from tests.fixtures.energy import energy_context_for, energy_horizon


def test_energy_context_is_complete_and_provider_is_read_only() -> None:
    context = energy_context_for()
    provider = StaticEnergyContextProvider(context)

    assert [point.slot for point in context.tariffs] == list(range(context.horizon.slots))
    assert provider.get_context(context.horizon) == context
    assert not hasattr(provider, "execute")


@pytest.mark.parametrize(
    "field",
    ["tariffs", "solar_forecast"],
)
def test_energy_context_rejects_missing_or_duplicate_slots(field: str) -> None:
    context = energy_context_for()
    points = list(getattr(context, field))[:-1]
    if field == "tariffs":
        payload = context.model_copy(update={"tariffs": points})
    else:
        payload = context.model_copy(update={"solar_forecast": points})

    with pytest.raises(ValidationError, match="must contain exactly one point"):
        EnergyContext.model_validate(payload.model_dump(mode="python"))


def test_energy_context_rejects_bad_units_and_battery_bounds() -> None:
    context = energy_context_for()
    payload = context.model_dump(mode="python")
    payload["solar_forecast"][0]["unit"] = "W"
    with pytest.raises(ValidationError, match="unit"):
        EnergyContext.model_validate(payload)

    invalid_battery = context.model_dump(mode="python")
    invalid_battery["battery"]["initial_soc_kwh"] = 99
    with pytest.raises(ValidationError, match="initial_soc"):
        EnergyContext.model_validate(invalid_battery)


def test_static_provider_rejects_a_different_horizon() -> None:
    provider = StaticEnergyContextProvider(energy_context_for())
    different = energy_horizon(slots=4)

    with pytest.raises(ValueError, match="horizon"):
        provider.get_context(different)
