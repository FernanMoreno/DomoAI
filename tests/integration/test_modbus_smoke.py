from __future__ import annotations

import os
from pathlib import Path

import pytest

from domoai.adapters.modbus.adapter import ModbusAdapter
from domoai.adapters.modbus.config import load_mapping
from domoai.adapters.modbus.transport import PyModbusTcpTransport


@pytest.mark.asyncio
async def test_live_modbus_smoke_is_opt_in() -> None:
    host = os.getenv("DOMOAI_MODBUS_HOST")
    config_path = os.getenv("DOMOAI_MODBUS_CONFIG_PATH")
    if not host or not config_path:
        pytest.skip(
            "Set DOMOAI_MODBUS_HOST and DOMOAI_MODBUS_CONFIG_PATH to run the live Modbus smoke test"
        )
    path = Path(config_path)
    if not path.exists():
        pytest.fail("DOMOAI_MODBUS_CONFIG_PATH must point to an existing mapping")
    adapter = ModbusAdapter(
        PyModbusTcpTransport(
            host,
            port=int(os.getenv("DOMOAI_MODBUS_PORT", "502")),
            timeout=float(os.getenv("DOMOAI_MODBUS_TIMEOUT_SECONDS", "5")),
        ),
        load_mapping(path),
        discovery_timeout=float(os.getenv("DOMOAI_MODBUS_TIMEOUT_SECONDS", "5")),
    )
    await adapter.connect()
    try:
        snapshot = await adapter.discover()
        assert snapshot.source_entities
    finally:
        await adapter.disconnect()
