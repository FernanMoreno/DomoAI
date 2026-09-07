"""Bounded FIFO work admission isolated by household."""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T")


class QueueOverloaded(RuntimeError):
    """A household or the total physical lane has no bounded capacity left."""


class QueueClosed(RuntimeError):
    """The runtime is shutting down and cannot accept new work."""


@dataclass
class _WorkItem:
    work: Callable[[], Any] | Awaitable[Any]
    future: asyncio.Future[Any]


class HouseholdWorkQueues:
    """Run physical work FIFO with independent household backpressure."""

    def __init__(self, *, max_per_household: int = 16, max_total: int = 64) -> None:
        if max_per_household <= 0 or max_total <= 0:
            raise ValueError("queue capacities must be positive")
        if max_per_household > max_total:
            raise ValueError("per-household capacity cannot exceed total capacity")
        self.max_per_household = max_per_household
        self.max_total = max_total
        self._queues: dict[str, deque[_WorkItem]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._counts: dict[str, int] = {}
        self._total = 0
        self._lock = asyncio.Lock()
        self._closed = False

    async def submit(
        self,
        household_id: str,
        work: Callable[[], _T | Awaitable[_T]] | Awaitable[_T],
    ) -> _T:
        if not household_id.strip():
            raise ValueError("household_id must be non-empty")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_T] = loop.create_future()
        async with self._lock:
            if self._closed:
                raise QueueClosed("household work queues are closed")
            count = self._counts.get(household_id, 0)
            if count >= self.max_per_household or self._total >= self.max_total:
                raise QueueOverloaded(f"household work queue is full for {household_id}")
            queue = self._queues.setdefault(household_id, deque())
            queue.append(_WorkItem(work=work, future=future))
            self._counts[household_id] = count + 1
            self._total += 1
            if household_id not in self._workers:
                self._workers[household_id] = asyncio.create_task(self._run(household_id))
        return await future

    def depths(self) -> dict[str, int]:
        return {household_id: count for household_id, count in self._counts.items() if count}

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            for queue in self._queues.values():
                while queue:
                    item = queue.popleft()
                    if not item.future.done():
                        item.future.set_exception(QueueClosed("household work queues are closed"))
            workers = tuple(self._workers.values())
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._lock:
            self._queues.clear()
            self._workers.clear()
            self._counts.clear()
            self._total = 0

    async def _run(self, household_id: str) -> None:
        while True:
            async with self._lock:
                queue = self._queues.get(household_id)
                if not queue:
                    self._workers.pop(household_id, None)
                    self._queues.pop(household_id, None)
                    return
                item = queue.popleft()
            try:
                result = item.work() if callable(item.work) else item.work
                if inspect.isawaitable(result):
                    result = await result
                if not item.future.done():
                    item.future.set_result(result)
            except BaseException as error:
                if not item.future.done():
                    item.future.set_exception(error)
            finally:
                async with self._lock:
                    self._counts[household_id] = self._counts.get(household_id, 1) - 1
                    self._total -= 1
                    if self._counts[household_id] <= 0:
                        self._counts.pop(household_id, None)
