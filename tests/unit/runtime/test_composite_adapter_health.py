from __future__ import annotations

import pytest

from domoai.runtime.composite_adapter import CompositeAdapter
from tests.fixtures.multi_adapter import RecordingAdapter, source_snapshot


def _adapter(adapter_id: str, *, fail_connect: bool = False) -> RecordingAdapter:
    return RecordingAdapter(
        adapter_id, source_snapshot(adapter_id=adapter_id), fail_connect=fail_connect
    )


@pytest.mark.asyncio
async def test_all_healthy_components_are_listed_as_connected() -> None:
    first = _adapter("home_assistant")
    second = _adapter("modbus")
    composite = CompositeAdapter([first, second])
    await composite.connect()

    health = await composite.health()

    assert health.connected is True
    assert health.components is not None
    component_ids = {component.adapter_id: component.connected for component in health.components}
    assert component_ids == {"home_assistant": True, "modbus": True}


@pytest.mark.asyncio
async def test_one_down_adapter_is_individually_identifiable() -> None:
    first = _adapter("home_assistant")
    second = _adapter("modbus", fail_connect=True)
    composite = CompositeAdapter([first, second])
    await composite.connect()

    health = await composite.health()

    assert health.connected is True
    assert health.components is not None
    down = [component for component in health.components if not component.connected]
    assert [component.adapter_id for component in down] == ["modbus"]


@pytest.mark.asyncio
async def test_two_down_adapters_are_both_individually_identifiable() -> None:
    first = _adapter("home_assistant", fail_connect=True)
    second = _adapter("modbus", fail_connect=True)
    third = _adapter("knx")
    composite = CompositeAdapter([first, second, third])
    await composite.connect()

    health = await composite.health()

    assert health.components is not None
    down_ids = {component.adapter_id for component in health.components if not component.connected}
    assert down_ids == {"home_assistant", "modbus"}
