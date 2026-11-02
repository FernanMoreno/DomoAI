import pytest

from domoai.adapters.modbus.transport import (
    InMemoryModbusTransport,
    ModbusIdentificationResponse,
)


@pytest.mark.asyncio
async def test_in_memory_device_identification_is_read_only_and_unit_bounded() -> None:
    transport = InMemoryModbusTransport(
        identifications={
            1: ModbusIdentificationResponse(
                vendor_name="Acme", product_code="meter-1", revision="1.2"
            )
        }
    )
    await transport.connect()

    result = await transport.identify_device(1)

    assert result is not None
    assert result.vendor_name == "Acme"
    assert transport.identification_reads == [1]
    assert transport.writes == []
    with pytest.raises(ValueError, match="unit"):
        await transport.identify_device(248)
