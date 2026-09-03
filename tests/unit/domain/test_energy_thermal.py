import pytest
from pydantic import ValidationError

from domoai.domain.energy import HVACActuator, ThermalProfile


def hvac_actuator(**overrides: object) -> HVACActuator:
    payload: dict[str, object] = {
        "device_id": "thermostat.home",
        "capability": "hvac_power",
        "heat_command": "heat",
        "cool_command": "cool",
        "stop_command": "stop",
        "power_feedback_capability": "hvac_power",
        "power_feedback_tolerance_kw": 0.1,
    }
    payload.update(overrides)
    return HVACActuator(**payload)


def thermal_profile(**overrides: object) -> ThermalProfile:
    payload: dict[str, object] = {
        "capacitance_kwh_per_c": 5.0,
        "ua_kw_per_c": 0.2,
        "initial_temperature_c": 20.0,
        "comfort_min_c": 19.0,
        "comfort_max_c": 22.0,
        "max_heat_kw": 3.0,
        "max_cool_kw": 2.0,
        "heating_cop": 3.0,
        "cooling_cop": 2.5,
    }
    payload.update(overrides)
    return ThermalProfile(**payload)


def test_hvac_actuator_rejects_non_distinct_commands() -> None:
    with pytest.raises(ValidationError, match="distinct"):
        hvac_actuator(cool_command="heat")


def test_hvac_actuator_rejects_poll_interval_exceeding_settle_timeout() -> None:
    with pytest.raises(ValidationError, match="poll interval"):
        hvac_actuator(
            power_feedback_settle_timeout_seconds=1.0,
            power_feedback_poll_interval_seconds=2.0,
        )


def test_hvac_actuator_accepts_valid_binding() -> None:
    actuator = hvac_actuator()
    assert actuator.heat_command == "heat"
    assert actuator.power_unit == "kW"


def test_thermal_profile_rejects_comfort_min_not_less_than_max() -> None:
    with pytest.raises(ValidationError, match="comfort_min_c"):
        thermal_profile(comfort_min_c=22.0, comfort_max_c=19.0)
    with pytest.raises(ValidationError, match="comfort_min_c"):
        thermal_profile(comfort_min_c=20.0, comfort_max_c=20.0)


def test_thermal_profile_accepts_valid_profile_without_actuator() -> None:
    profile = thermal_profile()
    assert profile.actuator is None
    assert profile.initial_temperature_c == 20.0


def test_thermal_profile_accepts_valid_profile_with_actuator() -> None:
    profile = thermal_profile(actuator=hvac_actuator())
    assert profile.actuator is not None
    assert profile.actuator.device_id == "thermostat.home"


def test_thermal_profile_allows_negative_initial_temperature() -> None:
    profile = thermal_profile(initial_temperature_c=-5.0, comfort_min_c=-2.0, comfort_max_c=5.0)
    assert profile.initial_temperature_c == -5.0
