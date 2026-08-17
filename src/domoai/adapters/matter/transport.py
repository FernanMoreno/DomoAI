"""Injectable Matter Server WebSocket transport boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class MatterTransportMessage:
    payload: dict[str, Any]


@dataclass(frozen=True)
class MatterRequest:
    command: str
    args: dict[str, Any] | None


class MatterTransport(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def request(self, command: str, args: dict[str, Any] | None = None) -> Any: ...

    async def receive(self, timeout: float | None = None) -> MatterTransportMessage | None: ...

    async def health(self) -> bool: ...


class InMemoryMatterTransport:
    """Deterministic Matter Server transport for tests and local fixtures."""

    def __init__(
        self,
        *,
        nodes: list[dict[str, Any]],
        server_info: dict[str, Any],
        responses: dict[str, Any] | None = None,
    ) -> None:
        self.nodes = nodes
        self.server_info = server_info
        self.responses = dict(responses or {})
        self.incoming: list[MatterTransportMessage] = []
        self.requests: list[MatterRequest] = []
        self.connected = False
        self._waiter = asyncio.Event()

    async def connect(self) -> None:
        try:
            _validate_schema_range(
                self.server_info, MatterServerWebSocketTransport.CLIENT_SCHEMA_VERSION
            )
        except ValueError as error:
            raise ConnectionError(str(error)) from error
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def request(self, command: str, args: dict[str, Any] | None = None) -> Any:
        if not self.connected:
            raise ConnectionError("Matter transport is not connected")
        self.requests.append(MatterRequest(command=command, args=args))
        if command == "start_listening":
            return self.nodes
        return self.responses.get(command)

    async def receive(self, timeout: float | None = None) -> MatterTransportMessage | None:
        if self.incoming:
            return self.incoming.pop(0)
        if timeout is None or timeout <= 0:
            return None
        self._waiter.clear()
        try:
            await asyncio.wait_for(self._waiter.wait(), timeout)
        except TimeoutError:
            return None
        return self.incoming.pop(0) if self.incoming else None

    async def health(self) -> bool:
        return self.connected

    def enqueue(self, message: MatterTransportMessage) -> None:
        self.incoming.append(message)
        self._waiter.set()


class MatterServerWebSocketTransport:
    """Live client for the Matter Server Python-compatible WebSocket API."""

    CLIENT_SCHEMA_VERSION = 13

    def __init__(self, url: str, *, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout
        self._socket: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[MatterTransportMessage] = asyncio.Queue()
        self._server_info: dict[str, Any] | None = None
        self._connected = False

    async def connect(self) -> None:
        try:
            from websockets.asyncio.client import connect

            self._socket = await connect(
                self.url,
                open_timeout=self.timeout,
                close_timeout=self.timeout,
                max_size=None,
            )
            raw_info = await asyncio.wait_for(self._socket.recv(), self.timeout)
            info = self._parse_json(raw_info)
            self._validate_server_info(info)
            self._server_info = info
            self._connected = True
            self._reader_task = asyncio.create_task(self._read_messages())
        except (ConnectionError, OSError, TimeoutError, ValueError) as error:
            await self.disconnect()
            raise ConnectionError(f"Matter Server connection failed: {error}") from error
        except ImportError as error:
            raise RuntimeError("websockets is required for the live Matter transport") from error
        except Exception as error:
            await self.disconnect()
            raise ConnectionError("Matter Server connection failed") from error

    async def disconnect(self) -> None:
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionError("Matter Server transport disconnected"))
        self._pending.clear()
        if self._socket is not None:
            try:
                await self._socket.close()
            except Exception:
                pass
        self._socket = None

    async def request(self, command: str, args: dict[str, Any] | None = None) -> Any:
        if not self._connected or self._socket is None:
            raise ConnectionError("Matter transport is not connected")
        message_id = str(uuid4())
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._socket.send(
                json.dumps(
                    {"message_id": message_id, "command": command, "args": args},
                    separators=(",", ":"),
                )
            )
            return await asyncio.wait_for(future, self.timeout)
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"Matter Server request failed: {error}") from error
        finally:
            self._pending.pop(message_id, None)

    async def receive(self, timeout: float | None = None) -> MatterTransportMessage | None:
        if not self._connected:
            raise ConnectionError("Matter transport is not connected")
        try:
            if timeout is None:
                return await self._events.get()
            return await asyncio.wait_for(self._events.get(), timeout)
        except TimeoutError:
            return None

    async def health(self) -> bool:
        return self._connected and self._socket is not None

    async def _read_messages(self) -> None:
        assert self._socket is not None
        try:
            async for raw_message in self._socket:
                try:
                    payload = self._parse_json(raw_message)
                except ValueError:
                    await self._events.put(
                        MatterTransportMessage(payload={"event": "malformed", "data": {}})
                    )
                    continue
                message_id = payload.get("message_id")
                if isinstance(message_id, str) and message_id in self._pending:
                    future = self._pending[message_id]
                    if future.done():
                        continue
                    if "error_code" in payload:
                        details = str(payload.get("details") or "Matter Server command failed")
                        future.set_exception(ConnectionError(details))
                    else:
                        future.set_result(payload.get("result"))
                elif "event" in payload:
                    await self._events.put(MatterTransportMessage(payload=payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            self._connected = False
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Matter Server connection closed"))
            await self._events.put(
                MatterTransportMessage(payload={"event": "connection_lost", "data": {}})
            )

    @staticmethod
    def _parse_json(raw_message: Any) -> dict[str, Any]:
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        payload = json.loads(raw_message)
        if not isinstance(payload, dict):
            raise ValueError("Matter Server message must be an object")
        return payload

    def _validate_server_info(self, info: dict[str, Any]) -> None:
        _validate_schema_range(info, self.CLIENT_SCHEMA_VERSION)


def _validate_schema_range(info: dict[str, Any], client_schema_version: int) -> None:
    schema_version = info.get("schema_version")
    minimum_schema_version = info.get("min_supported_schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or isinstance(minimum_schema_version, bool)
        or not isinstance(minimum_schema_version, int)
    ):
        raise ValueError("Matter Server info does not contain a valid schema range")
    if not minimum_schema_version <= client_schema_version <= schema_version:
        raise ValueError(
            f"Matter Server schema range is incompatible with client schema {client_schema_version}"
        )
