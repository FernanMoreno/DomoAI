import pytest

from domoai.adapters.modbus.connection import ModbusConnectionProvider
from domoai.adapters.modbus.transport import InMemoryModbusTransport, ModbusIdentificationResponse


@pytest.mark.asyncio
async def test_modbus_connection_probe_is_read_only() -> None:
    transport = InMemoryModbusTransport(
        identifications={2: ModbusIdentificationResponse("Acme", "sensor", "1.0")}
    )
    provider = ModbusConnectionProvider(
        transport_factory=lambda _host, _port: transport,
        unit_ids=(2,),
    )
    candidate = (await provider.discover())[0]

    result = await provider.probe(candidate)

    assert result.product_status.value in {"connected", "needs_setup"}
    assert transport.writes == []
    assert transport.identification_reads == [2]
