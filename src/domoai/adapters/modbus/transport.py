"""Injectable Modbus transport boundaries and deterministic fixtures."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from domoai.adapters.modbus.config import ModbusArea
from domoai.runtime.clock import Clock, SystemClock
from domoai.runtime.execution_context import ExecutionContext

RawValue = bool | int


@dataclass(frozen=True)
class ModbusSample:
    unit_id: int
    area: ModbusArea
    address: int
    values: tuple[RawValue, ...]
    observed_at: datetime


@dataclass(frozen=True)
class ModbusWrite:
    unit_id: int
    area: ModbusArea
    address: int
    values: tuple[RawValue, ...]
    execution_context: ExecutionContext | None = None


class ModbusTransport(Protocol):
    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def read(
        self, unit_id: int, area: ModbusArea, address: int, count: int
    ) -> ModbusSample | None: ...

    async def write(
        self,
        unit_id: int,
        area: ModbusArea,
        address: int,
        values: tuple[RawValue, ...],
        *,
        execution_context: ExecutionContext | None = None,
    ) -> None: ...

    async def health(self) -> bool: ...


class InMemoryModbusTransport:
    """Deterministic Modbus transport for contract and integration tests."""

    def __init__(
        self, samples: Sequence[ModbusSample] | None = None, *, clock: Clock | None = None
    ) -> None:
        self.samples = list(samples or [])
        self._clock = clock or SystemClock()
        self.reads: list[tuple[int, ModbusArea, int, int]] = []
        self.writes: list[ModbusWrite] = []
        self.connected = False
        self.healthy = True
        self.read_errors: dict[tuple[int, ModbusArea, int], BaseException] = {}
        self._values = {
            (sample.unit_id, sample.area, sample.address): sample for sample in self.samples
        }
        self.write_state_map: dict[tuple[int, ModbusArea, int], tuple[int, ModbusArea, int]] = {}
        self._waiter = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        self._waiter.set()

    async def read(
        self, unit_id: int, area: ModbusArea, address: int, count: int
    ) -> ModbusSample | None:
        self._require_connected()
        self.reads.append((unit_id, area, address, count))
        error = self.read_errors.get((unit_id, area, address))
        if error is not None:
            raise error
        return self._values.get((unit_id, area, address))

    async def write(
        self,
        unit_id: int,
        area: ModbusArea,
        address: int,
        values: tuple[RawValue, ...],
        *,
        execution_context: ExecutionContext | None = None,
    ) -> None:
        self._require_connected()
        if not self.healthy:
            raise ConnectionError("Modbus fixture is unhealthy")
        if area not in {"coil", "holding_register"}:
            raise ValueError("Modbus fixture cannot write this area")
        write = ModbusWrite(unit_id, area, address, values, execution_context)
        self.writes.append(write)
        sample = ModbusSample(unit_id, area, address, values, self._clock.now())
        self._values[(unit_id, area, address)] = sample
        state_key = self.write_state_map.get((unit_id, area, address))
        if state_key is not None:
            self._values[state_key] = ModbusSample(
                state_key[0], state_key[1], state_key[2], values, self._clock.now()
            )
        self._waiter.set()

    async def health(self) -> bool:
        return self.connected and self.healthy

    def enqueue(self, sample: ModbusSample) -> None:
        self.samples.append(sample)
        self._values[(sample.unit_id, sample.area, sample.address)] = sample
        self._waiter.set()

    def set_health(self, healthy: bool) -> None:
        self.healthy = healthy
        self._waiter.set()

    def _require_connected(self) -> None:
        if not self.connected:
            raise ConnectionError("Modbus transport is not connected")


ClientFactory = Callable[[str, int, float], Any]


class PyModbusTcpTransport:
    """Live Modbus TCP transport backed by the optional PyModbus client."""

    def __init__(
        self,
        host: str,
        *,
        port: int = 502,
        timeout: float = 5.0,
        client_factory: ClientFactory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._clock = clock or SystemClock()
        self._client_factory = client_factory
        self._client: Any = None
        self._lock = asyncio.Lock()
        self.last_write_context: ExecutionContext | None = None

    async def connect(self) -> None:
        try:
            factory = self._client_factory or _default_client_factory
            self._client = factory(self.host, self.port, self.timeout)
            result = self._client.connect()
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, self.timeout)
            if result is False and not getattr(self._client, "connected", False):
                raise ConnectionError("Modbus TCP client rejected the connection")
        except (ConnectionError, OSError, TimeoutError) as error:
            await self.disconnect()
            raise ConnectionError("Modbus TCP connection failed") from error
        except Exception as error:
            await self.disconnect()
            raise ConnectionError("Modbus TCP connection failed") from error

    async def disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            result = client.close()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass

    async def read(
        self, unit_id: int, area: ModbusArea, address: int, count: int
    ) -> ModbusSample | None:
        async with self._lock:
            client = self._require_client()
            try:
                method_name = {
                    "coil": "read_coils",
                    "discrete_input": "read_discrete_inputs",
                    "input_register": "read_input_registers",
                    "holding_register": "read_holding_registers",
                }[area]
                response = await self._request(client, method_name, address, count, unit_id)
                if response is None or response.isError():
                    raise ConnectionError("Modbus server returned a read exception")
                values: tuple[RawValue, ...]
                if area in {"coil", "discrete_input"}:
                    values = tuple(bool(value) for value in response.bits[:count])
                else:
                    values = tuple(int(value) for value in response.registers[:count])
                return ModbusSample(unit_id, area, address, values, self._clock.now())
            except (ConnectionError, OSError, TimeoutError, ValueError) as error:
                raise ConnectionError("Modbus read failed") from error
            except Exception as error:
                raise ConnectionError("Modbus read failed") from error

    async def write(
        self,
        unit_id: int,
        area: ModbusArea,
        address: int,
        values: tuple[RawValue, ...],
        *,
        execution_context: ExecutionContext | None = None,
    ) -> None:
        async with self._lock:
            client = self._require_client()
            try:
                if area == "coil":
                    if len(values) != 1 or not isinstance(values[0], bool):
                        raise ValueError("Modbus coil write requires one boolean")
                    response = await self._request(
                        client, "write_coil", address, values[0], unit_id
                    )
                elif area == "holding_register":
                    if any(
                        isinstance(value, bool) or not isinstance(value, int) for value in values
                    ):
                        raise ValueError("Modbus register write requires integer registers")
                    if len(values) == 1:
                        response = await self._request(
                            client, "write_register", address, values[0], unit_id
                        )
                    else:
                        response = await self._request(
                            client, "write_registers", address, list(values), unit_id
                        )
                else:
                    raise ValueError("Modbus writes are limited to coil and holding_register")
                if response is not None and response.isError():
                    raise ConnectionError("Modbus server returned a write exception")
                self.last_write_context = execution_context
            except (ConnectionError, OSError, TimeoutError, ValueError) as error:
                raise ConnectionError("Modbus write failed") from error
            except Exception as error:
                raise ConnectionError("Modbus write failed") from error

    async def health(self) -> bool:
        return self._client is not None and bool(getattr(self._client, "connected", True))

    async def _request(self, client: Any, method_name: str, *args: Any) -> Any:
        method = getattr(client, method_name)
        unit_id = args[-1]
        operation_args = args[:-1]
        try:
            result = method(*operation_args, slave=unit_id)
        except TypeError as error:
            if method_name.startswith("read_") and len(operation_args) == 2:
                # PyModbus 3.x made read count keyword-only and renamed the
                # unit selector to device_id.  Keep the legacy call above so
                # older clients and the existing contract remain supported,
                # then adapt only this read shape for current clients.
                result = method(
                    operation_args[0], count=operation_args[1], device_id=unit_id
                )
            else:
                if "slave" not in str(error):
                    raise
                result = method(*operation_args, device_id=unit_id)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, self.timeout)
        return result

    def _require_client(self) -> Any:
        if self._client is None:
            raise ConnectionError("Modbus TCP transport is not connected")
        return self._client


def _default_client_factory(host: str, port: int, timeout: float) -> Any:
    from pymodbus.client import AsyncModbusTcpClient

    return AsyncModbusTcpClient(host, port=port, timeout=timeout)
