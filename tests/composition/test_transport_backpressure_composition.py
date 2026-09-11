from __future__ import annotations

import pytest

from domoai.adapters.knx.transport import XknxTransport
from domoai.adapters.matter.transport import MatterServerWebSocketTransport


@pytest.mark.composition
def test_live_protocol_transports_expose_independent_bounded_buffers() -> None:
    matter = MatterServerWebSocketTransport(
        "ws://matter.example", max_frame_bytes=128, event_queue_max_size=2
    )
    knx = XknxTransport("192.0.2.10", event_queue_max_size=3)

    assert matter.max_frame_bytes == 128
    assert matter.event_queue_max_size == 2
    assert matter.event_queue_depth == 0
    assert matter.dropped_event_count == 0
    assert knx.event_queue_max_size == 3
    assert knx.event_queue_depth == 0
    assert knx.dropped_event_count == 0
