from datetime import UTC, datetime

from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.domain.p1_completion import ModbusDeviceIdentity
from tests.fixtures.modbus import mapping_payload


def test_modbus_profile_has_stable_signature_for_equivalent_mapping_json() -> None:
    mapping = ModbusMappingDocument.model_validate(mapping_payload(count=1))
    left = ModbusProfile(
        profile_id="fixture-dimmer",
        profile_version="1.0.0",
        vendor_name="Fixture Modbus",
        product_code="Dimmer 1",
        mapping=mapping,
        provenance="repo:data/modbus-profiles/fixture.json",
    )
    right = ModbusProfile.model_validate(left.model_dump(mode="json"))

    assert left.signature == right.signature
    assert len(left.signature) == 64


def test_identity_is_a_versioned_profile_input() -> None:
    identity = ModbusDeviceIdentity(
        host_ref="modbus:fixture",
        unit_id=1,
        vendor_name="Fixture Modbus",
        product_code="Dimmer 1",
        revision="1.2.0",
        complete=True,
        observed_at=datetime.now(UTC),
    )
    assert identity.product_code == "Dimmer 1"
