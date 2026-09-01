from __future__ import annotations

import asyncio
import os
import socket
import struct

import pytest

from domoai.adapters.knx.transport import XknxTransport


def _search_gateway(host: str, port: int) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, port))
        local_host, _local_port = probe.getsockname()

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(3.0)
        client.bind((local_host, 0))
        hpai = (
            b"\x08\x01"
            + b"\x00\x00\x00\x00"
            + b"\x00\x00"
        )
        request = b"\x06\x10\x02\x01" + struct.pack("!H", 6 + len(hpai)) + hpai
        client.sendto(request, (host, port))
        response, _source = client.recvfrom(4096)
        return response


def test_knx_gateway_discovery_and_tunnel_are_live() -> None:
    if os.getenv("DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE") != "1":
        pytest.skip("set DOMOAI_LIVE_BATTERY_KNX_GATEWAY_ENABLE=1 for the real KNX gateway")

    host = os.getenv("DOMOAI_KNX_GATEWAY_HOST", "172.26.80.1")
    port = int(os.getenv("DOMOAI_KNX_GATEWAY_PORT", "3672"))
    response = _search_gateway(host, port)

    assert response[2:4] == b"\x02\x02"
    assert b"domoai-knx-gateway" in response

    async def verify_tunnel() -> None:
        transport = XknxTransport(
            host,
            gateway_port=port,
            route_back=os.getenv("DOMOAI_KNX_ROUTE_BACK", "0").strip().lower()
            in {"1", "true", "yes", "on"},
            group_dpts={"4/0/0": "9.024"},
        )
        await transport.connect()
        try:
            assert await transport.health()
            await asyncio.sleep(2.0)
            assert await transport.health()
        finally:
            await transport.disconnect()

    asyncio.run(verify_tunnel())
