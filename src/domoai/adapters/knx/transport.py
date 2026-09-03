"""Injectable KNX transport boundary and deterministic fixture transport."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext

KnxScalar = bool | int | float


@dataclass(frozen=True)
class KnxGroupValue:
    group_address: str
    dpt: str
    value: KnxScalar
    observed_at: datetime


@dataclass(frozen=True)
class KnxWrite:
    group_address: str
    dpt: str
    value: KnxScalar
    execution_context: ExecutionContext | None = None


class KnxTransport(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def read_group(self, group_address: str, dpt: str) -> KnxGroupValue | None: ...

    async def write_group(
        self,
        group_address: str,
        dpt: str,
        value: KnxScalar,
        *,
        execution_context: ExecutionContext | None = None,
    ) -> None: ...

    async def receive(self, timeout: float | None = None) -> KnxGroupValue | None: ...

    async def health(self) -> bool: ...

    def set_group_read_response(
        self, group_address: str, dpt: str, value: KnxScalar
    ) -> None: ...


class InMemoryKnxTransport:
    """Deterministic KNX transport for contract and integration tests."""

    def __init__(
        self, incoming: Sequence[KnxGroupValue] | None = None, *, clock: Clock | None = None
    ) -> None:
        self.incoming = list(incoming or [])
        self._clock = clock or SystemClock()
        self.reads: list[tuple[str, str]] = []
        self.writes: list[KnxWrite] = []
        self.connected = False
        self.healthy = True
        self._values = {(value.group_address, value.dpt): value for value in self.incoming}
        self.write_state_map: dict[tuple[str, str], tuple[str, str]] = {}
        self._waiter = asyncio.Event()

    def set_group_read_response(
        self, group_address: str, dpt: str, value: KnxScalar
    ) -> None:
        self._values[(group_address, dpt)] = KnxGroupValue(
            group_address=group_address,
            dpt=dpt,
            value=value,
            observed_at=self._clock.now(),
        )

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def read_group(self, group_address: str, dpt: str) -> KnxGroupValue | None:
        self._require_connected()
        self.reads.append((group_address, dpt))
        return self._values.get((group_address, dpt))

    async def write_group(
        self,
        group_address: str,
        dpt: str,
        value: KnxScalar,
        *,
        execution_context: ExecutionContext | None = None,
    ) -> None:
        self._require_connected()
        self.writes.append(KnxWrite(group_address, dpt, value, execution_context))
        self._values[(group_address, dpt)] = KnxGroupValue(
            group_address=group_address,
            dpt=dpt,
            value=value,
            observed_at=self._clock.now(),
        )
        state_key = self.write_state_map.get((group_address, dpt))
        if state_key is not None:
            self._values[state_key] = KnxGroupValue(
                group_address=state_key[0],
                dpt=state_key[1],
                value=value,
                observed_at=self._clock.now(),
            )

    async def receive(self, timeout: float | None = None) -> KnxGroupValue | None:
        self._require_connected()
        if self.incoming:
            value = self.incoming.pop(0)
            self._values[(value.group_address, value.dpt)] = value
            return value
        if timeout is None or timeout <= 0:
            return None
        self._waiter.clear()
        try:
            await asyncio.wait_for(self._waiter.wait(), timeout)
        except TimeoutError:
            return None
        if not self.incoming:
            return None
        value = self.incoming.pop(0)
        self._values[(value.group_address, value.dpt)] = value
        return value

    async def health(self) -> bool:
        return self.connected and self.healthy

    def enqueue(self, value: KnxGroupValue) -> None:
        self.incoming.append(value)
        self._waiter.set()

    def set_health(self, healthy: bool) -> None:
        self.healthy = healthy
        self._waiter.set()

    def _require_connected(self) -> None:
        if not self.connected:
            raise ConnectionError("KNX transport is not connected")


class XknxTransport:
    """Live KNX/IP transport backed by the optional xknx dependency."""

    def __init__(
        self,
        gateway_host: str,
        *,
        timeout: float = 5.0,
        gateway_port: int = 3671,
        route_back: bool = False,
        group_dpts: dict[str, str] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.gateway_host = gateway_host
        self.timeout = timeout
        self.gateway_port = gateway_port
        self.route_back = route_back
        self._clock = clock or SystemClock()
        self.group_dpts = dict(group_dpts or {})
        self.writes: list[KnxWrite] = []
        self._xknx: Any = None
        self._callback: Any = None
        self._events: asyncio.Queue[KnxGroupValue] = asyncio.Queue()
        self._read_responses: dict[str, tuple[str, KnxScalar]] = {}

    def set_group_read_response(
        self, group_address: str, dpt: str, value: KnxScalar
    ) -> None:
        """Register the value returned when the lab facade receives a read.

        This is deliberately a transport-level lab hook.  KNX Virtual has no
        physical device object for the battery groups, so the host-side
        facade must answer GroupValueRead telegrams itself.
        """

        self._read_responses[group_address] = (dpt, value)

    async def connect(self) -> None:
        # Reconnects can be requested after the adapter has lost physical
        # availability while the KNXnet/IP session object is still present.
        # Close that session before replacing it; otherwise the old tunnel
        # keeps its callback/socket and the next discovery can remain
        # unavailable indefinitely.
        if self._xknx is not None:
            await self.disconnect()
        try:
            from xknx import XKNX
            from xknx.io import ConnectionConfig, ConnectionType
            from xknx.telegram.address import parse_device_group_address

            self._xknx = XKNX(
                connection_config=ConnectionConfig(
                    connection_type=ConnectionType.TUNNELING,
                    gateway_ip=self.gateway_host,
                    gateway_port=self.gateway_port,
                    route_back=self.route_back,
                    auto_reconnect=False,
                )
            )
            self._xknx.group_address_dpt.set(self.group_dpts)
            addresses = [parse_device_group_address(address) for address in self.group_dpts]
            self._callback = self._xknx.telegram_queue.register_telegram_received_cb(
                self._on_telegram,
                group_addresses=addresses or None,
            )
            await asyncio.wait_for(self._xknx.start(), self.timeout)
        except ImportError as error:
            await self.disconnect()
            raise RuntimeError("xknx is required for the live KNX transport") from error
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            await self.disconnect()
            raise ConnectionError("KNX/IP gateway connection failed") from error
        except Exception as error:
            await self.disconnect()
            raise ConnectionError("KNX/IP gateway connection failed") from error

    async def disconnect(self) -> None:
        xknx = self._xknx
        self._xknx = None
        if xknx is None:
            return
        if self._callback is not None:
            try:
                xknx.telegram_queue.unregister_telegram_received_cb(self._callback)
            except (ValueError, AttributeError):
                pass
            self._callback = None
        try:
            await asyncio.wait_for(xknx.stop(), self.timeout)
        except Exception:
            pass

    async def read_group(self, group_address: str, dpt: str) -> KnxGroupValue | None:
        xknx = self._require_xknx()
        try:
            from xknx.tools import read_group_value

            value = await asyncio.wait_for(
                read_group_value(xknx, group_address, value_type=dpt), self.timeout
            )
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            raise ConnectionError("KNX group read failed") from error
        if value is None:
            return None
        return KnxGroupValue(group_address, dpt, _enum_value(value), self._clock.now())

    async def write_group(
        self,
        group_address: str,
        dpt: str,
        value: KnxScalar,
        *,
        execution_context: ExecutionContext | None = None,
    ) -> None:
        xknx = self._require_xknx()
        try:
            from xknx.tools import group_value_write

            group_value_write(xknx, group_address, value, value_type=dpt)
            await asyncio.wait_for(xknx.join(), self.timeout)
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            raise ConnectionError("KNX group write failed") from error
        self.writes.append(KnxWrite(group_address, dpt, value, execution_context))

    async def receive(self, timeout: float | None = None) -> KnxGroupValue | None:
        self._require_xknx()
        try:
            if timeout is None:
                return await self._events.get()
            return await asyncio.wait_for(self._events.get(), timeout)
        except TimeoutError:
            return None

    async def health(self) -> bool:
        return self._xknx is not None and self._xknx.started.is_set()

    def _on_telegram(self, telegram: Any) -> None:
        destination = getattr(telegram, "destination_address", None)
        payload = getattr(telegram, "payload", None)
        decoded = getattr(telegram, "decoded_data", None)
        if destination is None:
            return
        from xknx.telegram.apci import GroupValueRead

        address = str(destination)
        if isinstance(payload, GroupValueRead):
            response = self._read_responses.get(address)
            if response is not None and self._xknx is not None:
                from xknx.tools import group_value_response

                dpt, value = response
                group_value_response(
                    self._xknx,
                    address,
                    value,
                    value_type=dpt,
                )
            return
        if decoded is None:
            return
        # xknx exposes GroupAddress.raw as the encoded integer (e.g. 2049),
        # while the mapper and KNX mapping contract use the textual address
        # (e.g. ``1/0/1``).  Preserve the protocol representation here.
        dpt = decoded.transcoder.dpt_number_str()
        self._events.put_nowait(
            KnxGroupValue(
                group_address=str(address),
                dpt=dpt,
                value=_enum_value(decoded.value),
                observed_at=self._clock.now(),
            )
        )

    def _require_xknx(self) -> Any:
        if self._xknx is None:
            raise ConnectionError("KNX transport is not connected")
        return self._xknx


def _enum_value(value: Any) -> KnxScalar:
    raw_value = getattr(value, "value", value)
    if isinstance(raw_value, bool | int | float):
        return raw_value
    raise ValueError("KNX transport returned an unsupported scalar value")
