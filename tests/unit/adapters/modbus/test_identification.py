import pytest

from domoai.adapters.modbus.identification import ModbusIdentificationService
from domoai.adapters.modbus.transport import InMemoryModbusTransport, ModbusIdentificationResponse


@pytest.mark.asyncio
async def test_identification_service_returns_complete_identity() -> None:
    transport = InMemoryModbusTransport(
        identifications={
            7: ModbusIdentificationResponse("Acme", "meter-1", "1.2")
        }
    )
    await transport.connect()

    identity = await ModbusIdentificationService(transport, unit_ids=(7,)).identify(
        "modbus:controller"
    )

    assert identity.complete is True
    assert identity.unit_id == 7


@pytest.mark.asyncio
async def test_identification_service_does_not_scan_unconfigured_units() -> None:
    transport = InMemoryModbusTransport()
    await transport.connect()

    identity = await ModbusIdentificationService(transport, unit_ids=(3,)).identify(
        "modbus:controller"
    )

    assert identity.complete is False
    assert transport.identification_reads == [3]
