from __future__ import annotations

import os
from pathlib import Path

import pytest

from domoai.adapters.knx.adapter import KnxAdapter
from domoai.adapters.knx.config import load_mapping
from domoai.adapters.knx.transport import XknxTransport


@pytest.mark.asyncio
async def test_live_knx_smoke_is_opt_in_and_read_only() -> None:
    host = os.getenv("DOMOAI_KNX_GATEWAY_HOST")
    mapping_path = os.getenv("DOMOAI_KNX_CONFIG_PATH")
    if not host or not mapping_path or not Path(mapping_path).exists():
        pytest.skip("KNX live smoke requires an explicit gateway and mapping file")

    timeout = float(os.getenv("DOMOAI_KNX_TIMEOUT_SECONDS", "5"))
    mapping = load_mapping(Path(mapping_path))
    group_dpts = {
        binding.state_group_address: binding.dpt
        for entity in mapping.entities
        for binding in entity.capabilities
    }
    transport = XknxTransport(host, timeout=timeout, group_dpts=group_dpts)
    adapter = KnxAdapter(transport, mapping, discovery_timeout=timeout)
    await adapter.connect()
    try:
        snapshot = await adapter.discover()
        assert snapshot.source_entities
        assert transport.writes == []
    finally:
        await adapter.disconnect()
