import pytest

from domoai.adapters.modbus.config import ModbusMappingDocument
from domoai.adapters.modbus.connection import ModbusConnectionProvider
from domoai.adapters.modbus.profiles import ModbusProfile
from domoai.adapters.modbus.transport import InMemoryModbusTransport, ModbusIdentificationResponse
from domoai.application.modbus_profiles import ModbusProfileCatalog
from domoai.domain.connections import ConnectionDraft
from domoai.runtime.connection_ports import ConnectionSetup
from domoai.runtime.managed_adapter_set import ManagedAdapterSet
from tests.fixtures.modbus import mapping_payload


@pytest.mark.composition
@pytest.mark.asyncio
async def test_modbus_identification_profile_and_runtime_activation_stay_bounded() -> None:
    transport = InMemoryModbusTransport(
        identifications={1: ModbusIdentificationResponse("Fixture Modbus", "Dimmer 1", "1.2.0")}
    )
    profile = ModbusProfile(
        profile_id="fixture-dimmer",
        profile_version="1.0.0",
        vendor_name="Fixture Modbus",
        product_code="Dimmer 1",
        mapping=ModbusMappingDocument.model_validate(mapping_payload(count=1)),
        provenance="repo:data/modbus-profiles/fixture.json",
        min_firmware="1.0.0",
        max_firmware="9.9.9",
    )
    provider = ModbusConnectionProvider(
        lambda _host, _port: transport,
        profile_catalog=ModbusProfileCatalog([profile]),
    )
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
    adapter = await provider.build_adapter(setup, adapter_id="modbus:managed")
    runtime = ManagedAdapterSet()

    await runtime.attach(adapter)
    await adapter.discover()

    assert transport.writes == []
    assert await runtime.detach("modbus:managed") is True
