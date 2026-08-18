"""Narrow MQTT transport boundary used by the Zigbee2MQTT adapter."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class MqttMessage:
    topic: str
    payload: bytes
    retained: bool = False


@dataclass(frozen=True)
class MqttPublish:
    topic: str
    payload: bytes
    retained: bool = False


class MqttTransport(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, topic_filter: str) -> None: ...

    async def publish(self, topic: str, payload: bytes, *, retained: bool = False) -> None: ...

    async def receive(self, timeout: float | None = None) -> MqttMessage | None: ...

    async def health(self) -> bool: ...


class InMemoryMqttTransport:
    """Deterministic MQTT transport for tests and local fixture runs."""

    def __init__(self, incoming: list[MqttMessage] | None = None) -> None:
        self.incoming = list(incoming or [])
        self.published: list[MqttPublish] = []
        self.subscriptions: list[str] = []
        self.connected = False
        self._waiter = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def subscribe(self, topic_filter: str) -> None:
        if not self.connected:
            raise ConnectionError("MQTT transport is not connected")
        self.subscriptions.append(topic_filter)

    async def publish(self, topic: str, payload: bytes, *, retained: bool = False) -> None:
        if not self.connected:
            raise ConnectionError("MQTT transport is not connected")
        self.published.append(MqttPublish(topic, payload, retained))

    async def receive(self, timeout: float | None = None) -> MqttMessage | None:
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

    def enqueue(self, message: MqttMessage) -> None:
        self.incoming.append(message)
        self._waiter.set()


class AiomqttTransport:
    """Optional live transport backed by the asyncio-native aiomqtt client."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 5.0,
        tls: bool = False,
        ca_cert_path: Path | None = None,
        client_cert_path: Path | None = None,
        client_key_path: Path | None = None,
        tls_insecure: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.tls = tls
        self.ca_cert_path = ca_cert_path
        self.client_cert_path = client_cert_path
        self.client_key_path = client_key_path
        self.tls_insecure = tls_insecure
        self._client: Any = None
        self._messages: AsyncIterator[Any] | None = None

    def _build_tls_context(self) -> ssl.SSLContext:
        context = ssl.create_default_context()
        if self.ca_cert_path is not None:
            context.load_verify_locations(cafile=str(self.ca_cert_path))
        if self.client_cert_path is not None and self.client_key_path is not None:
            context.load_cert_chain(
                certfile=str(self.client_cert_path),
                keyfile=str(self.client_key_path),
            )
        if self.tls_insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def connect(self) -> None:
        try:
            import aiomqtt

            self._client = aiomqtt.Client(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=self.timeout,
                tls_context=self._build_tls_context() if self.tls else None,
            )
            await asyncio.wait_for(self._client.__aenter__(), self.timeout)
            self._messages = self._client.messages.__aiter__()
        except (ConnectionError, OSError, TimeoutError) as error:
            raise ConnectionError(f"MQTT broker connection failed: {error}") from error
        except ImportError as error:
            raise RuntimeError("aiomqtt is required for live MQTT transport") from error
        except Exception as error:
            raise ConnectionError("MQTT broker connection failed") from error

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.__aexit__(None, None, None)
        self._client = None
        self._messages = None

    async def subscribe(self, topic_filter: str) -> None:
        if self._client is None:
            raise ConnectionError("MQTT transport is not connected")
        try:
            await self._client.subscribe(topic_filter)
        except Exception as error:
            raise ConnectionError("MQTT subscription failed") from error

    async def publish(self, topic: str, payload: bytes, *, retained: bool = False) -> None:
        if self._client is None:
            raise ConnectionError("MQTT transport is not connected")
        try:
            await self._client.publish(topic, payload=payload, retain=retained)
        except Exception as error:
            raise ConnectionError("MQTT publish failed") from error

    async def receive(self, timeout: float | None = None) -> MqttMessage | None:
        if self._messages is None:
            raise ConnectionError("MQTT transport is not connected")
        try:
            message = await asyncio.wait_for(anext(self._messages), timeout)
        except TimeoutError:
            return None
        except (ConnectionError, OSError) as error:
            raise ConnectionError("MQTT receive failed") from error
        return MqttMessage(
            topic=str(message.topic),
            payload=bytes(message.payload),
            retained=bool(getattr(message, "retain", False)),
        )

    async def health(self) -> bool:
        return self._client is not None
