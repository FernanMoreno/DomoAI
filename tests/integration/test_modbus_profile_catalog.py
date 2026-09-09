from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.application.modbus_profiles import ModbusProfileCatalog
from domoai.domain.p1_completion import ModbusDeviceIdentity, ProfileResolutionStatus
from tests.fixtures.modbus import mapping_payload


def test_unknown_device_never_produces_an_active_mapping() -> None:
    profile = ModbusProfile(
        profile_id="fixture-dimmer",
        profile_version="1.0.0",
        vendor_name="Fixture Modbus",
        product_code="Dimmer 1",
        mapping=ModbusMappingDocument.model_validate(mapping_payload(count=1)),
        provenance="repo:data/modbus-profiles/fixture.json",
    )
    result = ModbusProfileCatalog([profile]).resolve(
        ModbusDeviceIdentity(
            host_ref="modbus:unknown",
            unit_id=1,
            vendor_name="Unknown",
            product_code="Unknown",
            revision="0.0",
            complete=True,
        )
    )

    assert result.status is ProfileResolutionStatus.PROFILE_REQUIRED
    assert result.profile_id is None
