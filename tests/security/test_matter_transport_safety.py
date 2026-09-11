from __future__ import annotations

import asyncio
import json

import pytest

from domoai.adapters.matter.transport import MatterServerWebSocketTransport, MatterTransportMessage


def test_matter_transport_rejects_non_positive_limits() -> None:
    with pytest.raises(ValueError, match="frame"):
        MatterServerWebSocketTransport("ws://matter.test", max_frame_bytes=0)
    with pytest.raises(ValueError, match="queue"):
        MatterServerWebSocketTransport("ws://matter.test", event_queue_max_size=0)


@pytest.mark.asyncio
async def test_matter_transport_uses_finite_frame_and_drop_newest_queue(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Socket:
        async def recv(self) -> str:
            return json.dumps({"schema_version": 13, "min_supported_schema_version": 13})

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self) -> None:
            return None

    async def fake_connect(_url: str, **kwargs: object) -> _Socket:
        captured.update(kwargs)
        return _Socket()

    import websockets.asyncio.client

    monkeypatch.setattr(websockets.asyncio.client, "connect", fake_connect)
    transport = MatterServerWebSocketTransport(
        "ws://matter.test", max_frame_bytes=64, event_queue_max_size=1
    )
    await transport.connect()
    try:
        transport._enqueue_event(MatterTransportMessage(payload={"event": "first"}))
        transport._enqueue_event(MatterTransportMessage(payload={"event": "second"}))
        assert captured["max_size"] == 64
        assert transport.event_queue_max_size == 1
        assert transport.event_queue_depth == 1
        assert transport.dropped_event_count == 1
    finally:
        await transport.disconnect()


@pytest.mark.asyncio
async def test_matter_reader_routes_command_response_when_event_queue_is_full() -> None:
    transport = MatterServerWebSocketTransport(
        "ws://matter.test", event_queue_max_size=1
    )
    transport._connected = True
    transport._events.put_nowait(MatterTransportMessage(payload={"event": "queued"}))
    response_future = asyncio.get_running_loop().create_future()
    transport._pending["request-1"] = response_future

    class _Socket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if not hasattr(self, "sent"):
                self.sent = True
                return json.dumps({"event": "flood"})
            if not hasattr(self, "responded"):
                self.responded = True
                return json.dumps({"message_id": "request-1", "result": {"ok": True}})
            raise StopAsyncIteration

    transport._socket = _Socket()
    await transport._read_messages()

    assert response_future.result() == {"ok": True}
    assert transport.event_queue_depth == 1
    assert transport.dropped_event_count == 1
