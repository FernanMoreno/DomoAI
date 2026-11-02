from __future__ import annotations

import pytest

from domoai.adapters.knx.transport import XknxTransport


def _telegram(value: bool) -> object:
    class _Destination:
        def __str__(self) -> str:
            return "1/0/1"

    class _Transcoder:
        def dpt_number_str(self) -> str:
            return "1.001"

    class _Decoded:
        transcoder = _Transcoder()

    class _Telegram:
        destination_address = _Destination()
        decoded_data = _Decoded()

    telegram = _Telegram()
    telegram.decoded_data.value = value
    return telegram


def test_knx_transport_rejects_non_positive_queue_limit() -> None:
    with pytest.raises(ValueError, match="queue"):
        XknxTransport("192.0.2.10", event_queue_max_size=0)


@pytest.mark.asyncio
async def test_knx_callback_drops_newest_event_and_exposes_bounded_metrics() -> None:
    transport = XknxTransport("192.0.2.10", event_queue_max_size=1)
    transport._xknx = object()

    transport._on_telegram(_telegram(True))
    transport._on_telegram(_telegram(False))

    assert transport.event_queue_max_size == 1
    assert transport.event_queue_depth == 1
    assert transport.dropped_event_count == 1
    event = await transport.receive(0.01)
    assert event is not None
    assert event.value is True
