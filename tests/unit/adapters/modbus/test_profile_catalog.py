
from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.application.modbus_profiles import ModbusProfileCatalog
from domoai.domain.p1_completion import (
    ModbusDeviceIdentity,
    ProfileResolutionStatus,
)
from tests.fixtures.modbus import mapping_payload


def _identity(**changes: object) -> ModbusDeviceIdentity:
    values: dict[str, object] = {
        "host_ref": "modbus:fixture",
        "unit_id": 1,
        "vendor_name": "Fixture Modbus",
        "product_code": "Dimmer 1",
        "revision": "1.2.0",
        "complete": True,
    }
    values.update(changes)
    return ModbusDeviceIdentity(**values)


def _profile(**changes: object) -> ModbusProfile:
    values: dict[str, object] = {
        "profile_id": "fixture-dimmer",
        "profile_version": "1.0.0",
        "vendor_name": "Fixture Modbus",
        "product_code": "Dimmer 1",
        "mapping": ModbusMappingDocument.model_validate(mapping_payload(count=1)),
        "provenance": "repo:data/modbus-profiles/fixture.json",
    }
    values.update(changes)
    return ModbusProfile(**values)


def test_catalog_matches_exact_identity_and_rejects_unknown() -> None:
    catalog = ModbusProfileCatalog([_profile()])

    match = catalog.resolve(_identity())
    unknown = catalog.resolve(_identity(product_code="Other"))

    assert match.status is ProfileResolutionStatus.MATCHED
    assert match.profile_id == "fixture-dimmer"
    assert unknown.status is ProfileResolutionStatus.PROFILE_REQUIRED
    assert unknown.profile_id is None


def test_catalog_returns_incompatible_for_firmware_constraint() -> None:
    catalog = ModbusProfileCatalog([_profile(min_firmware="2.0.0")])

    result = catalog.resolve(_identity())

    assert result.status is ProfileResolutionStatus.INCOMPATIBLE
