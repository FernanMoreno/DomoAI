import os

import pytest

from domoai.adapters.matter.adapter import MatterServerAdapter
from domoai.adapters.matter.transport import MatterServerWebSocketTransport


@pytest.mark.asyncio
async def test_live_matter_server_smoke_is_opt_in() -> None:
    url = os.getenv("DOMOAI_MATTER_SERVER_URL")
    if not url:
        pytest.skip("DOMOAI_MATTER_SERVER_URL is not configured")
    transport = MatterServerWebSocketTransport(url, timeout=5)
    adapter = MatterServerAdapter(transport, discovery_timeout=5)
    await adapter.connect()
    try:
        snapshot = await adapter.discover()
        assert snapshot.source_entities is not None
    finally:
        await adapter.disconnect()
