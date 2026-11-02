import pytest

from domoai.adapters.modbus.identification import ModbusIdentificationService
from domoai.adapters.modbus.transport import InMemoryModbusTransport


@pytest.mark.asyncio
async def test_identification_rejects_aggressive_unit_id_policy() -> None:
    transport = InMemoryModbusTransport()
    with pytest.raises(ValueError, match="bounded"):
        ModbusIdentificationService(transport, unit_ids=tuple(range(1, 248)))
