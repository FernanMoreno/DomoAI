import os

import pytest

from domoai.adapters.home_assistant.adapter import HomeAssistantAdapter
from domoai.adapters.home_assistant.client import HomeAssistantClient


@pytest.mark.asyncio
async def test_home_assistant_discovery_read_reconnect_smoke() -> None:
    base_url = os.getenv("DOMOAI_HOME_ASSISTANT_URL")
    token = os.getenv("DOMOAI_HOME_ASSISTANT_TOKEN")
    if not base_url or not token:
        pytest.skip("Set DOMOAI_HOME_ASSISTANT_URL and DOMOAI_HOME_ASSISTANT_TOKEN for HA smoke")

    adapter = HomeAssistantAdapter(HomeAssistantClient(base_url, token))
    await adapter.connect()
    snapshot = await adapter.discover()
    assert snapshot.source_entities
    await adapter.disconnect()
