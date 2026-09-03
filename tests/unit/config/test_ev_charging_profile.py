import json
from pathlib import Path

import pytest

from domoai.config.ev_charging_profile import (
    EVChargingProfileConfigurationError,
    load_ev_charging_binding,
)
from domoai.domain.energy import EVChargingBinding


def _binding_payload() -> dict[str, object]:
    return {
        "provider_id": "ev_fixture",
        "device_id": "ev.home",
        "actuator": {
            "device_id": "ev.home",
            "capability": "ev_charging",
            "charge_command": "charge_ev",
            "stop_command": "stop_ev",
            "connected_capability": "ev.connected",
            "departure_capability": "ev.departure_at",
            "max_charge_kw": 7.4,
        },
        "soc_capability": "ev.soc",
        "capacity_capability": "ev.capacity",
    }


def test_load_ev_charging_binding_round_trips_valid_document(tmp_path: Path) -> None:
    path = tmp_path / "ev-charging-binding.json"
    path.write_text(json.dumps(_binding_payload()), encoding="utf-8")

    binding = load_ev_charging_binding(path)

    assert isinstance(binding, EVChargingBinding)
    assert binding.device_id == "ev.home"
    assert binding.actuator.max_charge_kw == 7.4


def test_load_ev_charging_binding_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EVChargingProfileConfigurationError):
        load_ev_charging_binding(tmp_path / "missing.json")


def test_load_ev_charging_binding_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(EVChargingProfileConfigurationError):
        load_ev_charging_binding(path)


def test_load_ev_charging_binding_rejects_schema_violation(tmp_path: Path) -> None:
    payload = _binding_payload()
    del payload["actuator"]
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EVChargingProfileConfigurationError):
        load_ev_charging_binding(path)


def test_lab_ev_profile_asset_is_valid_simulation_profile() -> None:
    from domoai.lab.ev_charging_simulator import EVChargingSimulationProfile

    payload = json.loads(Path("dev/lab/ev-charger/profile.json").read_text(encoding="utf-8"))
    profile = EVChargingSimulationProfile.from_dict(payload)

    assert profile.device_id == "lab-ev-1"
