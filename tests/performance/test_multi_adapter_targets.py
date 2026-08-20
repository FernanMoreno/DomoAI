from __future__ import annotations

import time

import pytest

from domoai.application.discovery_service import DiscoveryService
from domoai.runtime.composite_adapter import CompositeAdapter
from domoai.runtime.events import AuditLog
from domoai.runtime.registry import DeviceRegistry
from domoai.runtime.state_store import StateStore
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


@pytest.mark.asyncio
async def test_two_adapter_20_entity_discovery_target() -> None:
    first = RecordingAdapter("first", source_snapshot(adapter_id="first", extra_entities=10))
    second = RecordingAdapter("second", source_snapshot(adapter_id="second", extra_entities=10))
    registry = DeviceRegistry()
    composite = CompositeAdapter([first, second], registry=registry)
    discovery = DiscoveryService(composite, registry, StateStore(), AuditLog())

    await composite.connect()
    started = time.perf_counter()
    result = await discovery.refresh()
    elapsed = time.perf_counter() - started

    assert len(result.devices) >= 20
    assert elapsed < 1.0
