import pytest

from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.connection import ModbusConnectionProvider
from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.adapters.modbus.transport import InMemoryModbusTransport, ModbusIdentificationResponse
from domoai.application.modbus_profiles import ModbusProfileCatalog
from domoai.domain.connections import ConnectionDraft
from domoai.runtime.connection_ports import ConnectionSetup
from tests.fixtures.modbus import mapping_payload


def _provider(identified: bool = True) -> ModbusConnectionProvider:
    transport = InMemoryModbusTransport(
        identifications=(
            {1: ModbusIdentificationResponse("Fixture Modbus", "Dimmer 1", "1.2.0")}
            if identified
            else {}
        )
    )
    profile = ModbusProfile(
        profile_id="fixture-dimmer",
        profile_version="1.0.0",
        vendor_name="Fixture Modbus",
        product_code="Dimmer 1",
        mapping=ModbusMappingDocument.model_validate(mapping_payload(count=1)),
        provenance="repo:data/modbus-profiles/fixture.json",
    )
    return ModbusConnectionProvider(
        transport_factory=lambda _host, _port: transport,
        profile_catalog=ModbusProfileCatalog([profile]),
    )


@pytest.mark.asyncio
async def test_modbus_provider_validates_identity_and_builds_catalog_profile() -> None:
    provider = _provider()
    candidate = (await provider.discover())[0]
    setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="modbus",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={},
        ),
    )

    validation = await provider.validate(setup)
    adapter = await provider.build_adapter(setup, adapter_id="modbus:garage")

    assert validation.valid is True
    assert adapter.adapter_id == "modbus:garage"


@pytest.mark.asyncio
async def test_modbus_provider_leaves_unknown_device_unconfigured() -> None:
    provider = _provider(identified=False)
    candidate = (await provider.discover())[0]
    setup = ConnectionSetup(
        candidate=candidate,
        draft=ConnectionDraft(
            connector_id="modbus",
            unique_id=candidate.candidate_id,
            endpoint=candidate.endpoint,
            options={},
        ),
    )

    result = await provider.validate(setup)

    assert result.valid is False
    assert result.diagnostics[0].code == "modbus_profile_required"
