from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.adapters.mqtt.config import MqttAdapterMapping
from domoai.adapters.mqtt.discovery import MqttDiscoveryPreview
from domoai.domain.p1_completion import (
    CommissioningMethod,
    CommissioningState,
    MatterCommissioningSession,
    ModbusDeviceIdentity,
    ModbusProfileResolution,
    ProfileResolutionStatus,
)
from domoai.runtime.connection_provider_loader import ConnectionProviderLoadDiagnostic

ROOT = Path(__file__).parents[2]


def _mqtt_preview() -> MqttDiscoveryPreview:
    mapping = MqttAdapterMapping.model_validate(
        {
            "schema_version": "v1",
            "adapter_id": "mqtt-discovery",
            "devices": [
                {
                    "source_id": "mqtt:lamp",
                    "type": "light",
                    "capabilities": [
                        {
                            "name": "power",
                            "state_topic": "home/lamp/state",
                            "command_topic": "home/lamp/set",
                            "commands": ["on", "off"],
                        }
                    ],
                }
            ],
        }
    )
    return MqttDiscoveryPreview(
        adapter_id="mqtt-discovery",
        mapping=mapping,
        mapping_digest="a" * 64,
        source_topics=("homeassistant/light/lamp/config",),
        persistable=True,
    )


@pytest.mark.parametrize(
    ("schema_name", "model_factory"),
    [
        (
            "matter-commissioning-session",
            lambda: MatterCommissioningSession(
                session_id="matter-commissioning:1",
                connection_id="connection:1",
                method=CommissioningMethod.MANUAL,
                state=CommissioningState.AWAITING_CONFIRMATION,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                deadline=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            ),
        ),
        (
            "modbus-device-identity",
            lambda: ModbusDeviceIdentity(
                host_ref="modbus:fixture",
                unit_id=1,
                vendor_name="Fixture Modbus",
                product_code="Dimmer 1",
                revision="1.2.0",
                complete=True,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        ),
        (
            "modbus-profile-resolution",
            lambda: ModbusProfileResolution(
                status=ProfileResolutionStatus.MATCHED,
                identity=ModbusDeviceIdentity(
                    host_ref="modbus:fixture",
                    unit_id=1,
                    vendor_name="Fixture Modbus",
                    product_code="Dimmer 1",
                    revision="1.2.0",
                    complete=True,
                    observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                profile_id="fixture-dimmer",
            ),
        ),
        (
            "modbus-profile",
            lambda: ModbusProfile.model_validate(
                json.loads(
                    (ROOT / "data/modbus-profiles/fixture-dimmer.json").read_text(
                        encoding="utf-8"
                    )
                )
            ),
        ),
        ("mqtt-discovery-preview", _mqtt_preview),
        (
            "connection-provider-load-diagnostic",
            lambda: ConnectionProviderLoadDiagnostic(
                code="provider_entry_point_invalid",
                entry_point="fixture",
                message="provider entry point was rejected",
            ),
        ),
    ],
)
def test_p1_model_dump_validates_against_exported_schema(
    schema_name: str, model_factory: object
) -> None:
    schema = json.loads(
        (ROOT / "schemas/v1" / f"{schema_name}.schema.json").read_text(encoding="utf-8")
    )
    model = model_factory()  # type: ignore[operator]

    Draft202012Validator(schema).validate(model.model_dump(mode="json"))
