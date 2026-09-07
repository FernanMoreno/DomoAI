from __future__ import annotations

import json
from pathlib import Path

import pytest

from domoai.config.ev_charging_profile import (
    EVChargingProfileConfigurationError,
    load_ev_charging_bindings,
)


def _payload() -> dict[str, object]:
    return {
        "schema_version": "v1",
        "provider_id": "fixture_ev",
        "device_id": "ev.garage",
        "capability": "ev.charge_power",
        "charge_command": "set_charge_power",
        "stop_command": "stop_charging",
        "connected_capability": "ev.connected",
        "soc_capability": "ev.soc",
        "power_feedback_capability": "ev.power",
        "departure_capability": "ev.departure_at",
        "capacity_kwh": 60.0,
        "max_charge_kw": 7.4,
    }


def test_load_ev_charging_binding_from_server_owned_json(tmp_path: Path) -> None:
    path = tmp_path / "ev-charging.json"
    path.write_text(json.dumps({"bindings": [_payload()]}), encoding="utf-8")

    bindings = load_ev_charging_bindings(path)

    assert len(bindings) == 1
    assert bindings[0].device_id == "ev.garage"


def test_invalid_ev_charging_profile_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ev-charging-invalid.json"
    path.write_text(
        json.dumps({"bindings": [{**_payload(), "max_charge_kw": 0}]}), encoding="utf-8"
    )

    with pytest.raises(EVChargingProfileConfigurationError):
        load_ev_charging_bindings(path)
