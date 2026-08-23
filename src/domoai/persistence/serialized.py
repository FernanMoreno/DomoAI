"""Bounded, serialized execution boundary for blocking persistence work."""

from __future__ import annotations

import asyncio
import inspect
import queue
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, TypeVar, cast

T = TypeVar("T")


class StorageError(RuntimeError):
    """Base class for server-owned storage admission/execution failures."""


class StorageOverloadedError(StorageError):
    """The bounded storage boundary could not admit an operation in time."""


class StorageOperationTimeoutError(StorageError):
    """The storage operation exceeded its server-owned execution deadline."""


@dataclass(frozen=True)
class StorageMetrics:
    operation_count: int
    completed_count: int
    failed_count: int
    timeout_count: int
    overloaded_count: int
    queue_depth: int
    max_in_flight: int
    total_queue_wait_seconds: float
    last_error: str | None


@dataclass
class _WorkItem:
    operation: Callable[[], Any]
    result: Future[Any]
    enqueued_at: float


class SerializedStorageExecutor:
    """Runs blocking storage operations on one dedicated thread.

    The queue and deadline are deployment-owned. A timed-out operation is not
    cancelled underneath SQLite: its result is drained by the worker before
    the slot is released. This prevents a caller from replaying an operation
    merely because its wait timed out.
    """

    def __init__(
        self,
        *,
        queue_capacity: int = 128,
        queue_wait_seconds: float = 0.25,
        operation_timeout_seconds: float = 5.0,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be at least 1")
        if queue_wait_seconds <= 0 or operation_timeout_seconds <= 0:
            raise ValueError("storage wait and operation timeouts must be positive")
        self.queue_wait_seconds = queue_wait_seconds
        self.operation_timeout_seconds = operation_timeout_seconds
        self._max_in_flight = queue_capacity + 1
        self._queue: queue.Queue[_WorkItem | None] = queue.Queue(maxsize=queue_capacity)
        self._slots = threading.BoundedSemaphore(self._max_in_flight)
        self._state_lock = threading.Lock()
        self._closed = False
        self._worker = threading.Thread(
            target=self._worker_main,
            name="domoai-storage",
            daemon=True,
        )
        self._operation_count = 0
        self._completed_count = 0
        self._failed_count = 0
        self._timeout_count = 0
        self._overloaded_count = 0
        self._total_queue_wait_seconds = 0.0
        self._last_error: str | None = None
        self._worker.start()

    def _acquire_slot(self, blocking: bool, timeout: float | None = None) -> bool:
        if timeout is None:
            return self._slots.acquire(blocking)
        return self._slots.acquire(blocking, timeout)

    def _release_slot(self, _: Future[Any] | None = None) -> None:
        self._slots.release()

    def _ensure_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise StorageError("storage executor is closed")

    def _enqueue(self, operation: Callable[[], Any]) -> Future[Any]:
        self._ensure_open()
        result: Future[Any] = Future()
        item = _WorkItem(operation=operation, result=result, enqueued_at=time.monotonic())
        try:
            self._queue.put_nowait(item)
        except queue.Full as error:
            with self._state_lock:
                self._overloaded_count += 1
            raise StorageOverloadedError("storage queue is full") from error
        with self._state_lock:
            self._operation_count += 1
        return result

    async def run(self, operation: Callable[[], T]) -> T:
        """Admit and await one synchronous operation without blocking the loop."""

        acquired = await asyncio.to_thread(
            self._acquire_slot, True, self.queue_wait_seconds
        )
        if not acquired:
            with self._state_lock:
                self._overloaded_count += 1
            raise StorageOverloadedError("storage queue admission timed out")
        try:
            result = self._enqueue(operation)
        except BaseException:
            self._release_slot()
            raise
        try:
            value = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(result)),
                timeout=self.operation_timeout_seconds,
            )
        except TimeoutError as error:
            with self._state_lock:
                self._timeout_count += 1
            result.add_done_callback(self._release_slot)
            raise StorageOperationTimeoutError("storage operation timed out") from error
        except asyncio.CancelledError:
            result.add_done_callback(self._release_slot)
            raise
        else:
            self._release_slot()
            return cast(T, value)

    async def run_async(self, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run an async-shaped repository operation on the storage thread."""

        return await self.run(lambda: asyncio.run(operation()))

    def submit_nowait(self, operation: Callable[[], T]) -> Future[T]:
        """Submit fire-and-forget work for synchronous audit sinks.

        The returned future is retained by callers that need delivery evidence;
        the runtime audit sink intentionally uses the bounded admission result
        as backpressure and drains the queue during shutdown.
        """

        self._ensure_open()
        if not self._acquire_slot(False):
            with self._state_lock:
                self._overloaded_count += 1
            raise StorageOverloadedError("storage queue is full")
        try:
            result = self._enqueue(operation)
        except BaseException:
            self._release_slot()
            raise
        result.add_done_callback(self._release_slot)
        return result

    def _worker_main(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                self._queue.task_done()
                return
            started = time.monotonic()
            with self._state_lock:
                self._total_queue_wait_seconds += started - item.enqueued_at
            try:
                value = item.operation()
            except BaseException as error:
                with self._state_lock:
                    self._failed_count += 1
                    self._last_error = f"{type(error).__name__}: {error}"
                item.result.set_exception(error)
            else:
                with self._state_lock:
                    self._completed_count += 1
                item.result.set_result(value)
            finally:
                self._queue.task_done()

    @property
    def metrics(self) -> StorageMetrics:
        with self._state_lock:
            return StorageMetrics(
                operation_count=self._operation_count,
                completed_count=self._completed_count,
                failed_count=self._failed_count,
                timeout_count=self._timeout_count,
                overloaded_count=self._overloaded_count,
                queue_depth=self._queue.qsize(),
                max_in_flight=self._max_in_flight,
                total_queue_wait_seconds=self._total_queue_wait_seconds,
                last_error=self._last_error,
            )

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.join()
        self._queue.put(None)
        self._worker.join()


class SerializedRepositoryProxy:
    """Duck-typed repository facade that offloads every method call."""

    def __init__(self, target: T, storage: SerializedStorageExecutor) -> None:
        self._target = target
        self._storage = storage

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if inspect.iscoroutinefunction(attribute):

            async def invoke(*args: Any, **kwargs: Any) -> Any:
                return await self._storage.run_async(
                    lambda: attribute(*args, **kwargs)
                )

            return invoke

        if callable(attribute):

            def submit(*args: Any, **kwargs: Any) -> None:
                self._storage.submit_nowait(lambda: attribute(*args, **kwargs))

            return submit
        return attribute
